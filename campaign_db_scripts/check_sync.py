"""
check_sync.py
--------------
Diagnostic checkpoint — run after any sync to verify data landed correctly.
Covers all performance tables with spend, impressions, clicks, and date ranges
per account.

Usage:
    python check_sync.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

print(f"\nDatabase: {DB_PATH}")
print()

# ── Structure tables ──────────────────────────────────────────────────────────
for table in ("campaigns", "ad_groups"):
    print(f"── {table} ──────────────────────────────────────────────────")
    rows = conn.execute(f"""
        SELECT
            a.account_name,
            a.platform_account_id,
            COUNT(*)                                               AS total,
            SUM(CASE WHEN t.status = 'ENABLED' THEN 1 ELSE 0 END) AS enabled,
            SUM(CASE WHEN t.status = 'PAUSED'  THEN 1 ELSE 0 END) AS paused
        FROM {table} t
        JOIN accounts a ON t.account_id = a.id
        GROUP BY t.account_id
        ORDER BY a.platform_account_id
    """).fetchall()

    if not rows:
        print("  (no data)")
    else:
        print(f"  {'Account':<25} {'Acct ID':<15} {'Total':>7} {'Enabled':>8} {'Paused':>7}")
        print(f"  {'─'*25} {'─'*15} {'─'*7} {'─'*8} {'─'*7}")
        for r in rows:
            print(
                f"  {r['account_name'][:24]:<25} "
                f"{r['platform_account_id']:<15} "
                f"{r['total']:>7,} "
                f"{r['enabled']:>8,} "
                f"{r['paused']:>7,}"
            )
    print()

# ── campaign_performance_daily ────────────────────────────────────────────────
print("── campaign_performance_daily  (device='ALL') ───────────────────")
rows = conn.execute("""
    SELECT
        a.account_name,
        a.platform_account_id,
        COUNT(*)                        AS rows,
        MIN(t.date)                     AS min_date,
        MAX(t.date)                     AS max_date,
        SUM(t.cost_micros) / 1e6        AS spend,
        SUM(t.impressions)              AS impressions,
        SUM(t.clicks)                   AS clicks,
        SUM(t.conversions)              AS conversions,
        AVG(t.search_impression_share)  AS avg_is
    FROM campaign_performance_daily t
    JOIN accounts a ON t.account_id = a.id
    WHERE t.device = 'ALL'
    GROUP BY t.account_id
    ORDER BY a.platform_account_id
""").fetchall()

if not rows:
    print("  (no data yet)")
else:
    print(f"  {'Account':<20} {'Min date':<12} {'Max date':<12} "
          f"{'Rows':>7} {'Spend':>12} {'Impr':>12} {'Clicks':>8} {'Conv':>7} {'Avg IS':>7}")
    print(f"  {'─'*20} {'─'*12} {'─'*12} "
          f"{'─'*7} {'─'*12} {'─'*12} {'─'*8} {'─'*7} {'─'*7}")
    for r in rows:
        avg_is = f"{r['avg_is']*100:.1f}%" if r['avg_is'] else "—"
        print(
            f"  {r['account_name'][:19]:<20} "
            f"{str(r['min_date']):<12} "
            f"{str(r['max_date']):<12} "
            f"{r['rows']:>7,} "
            f"${r['spend']:>11,.0f} "
            f"{r['impressions']:>12,} "
            f"{r['clicks']:>8,} "
            f"{r['conversions']:>7,.0f} "
            f"{avg_is:>7}"
        )
print()

# ── campaign_performance_hourly ───────────────────────────────────────────────
print("── campaign_performance_hourly ──────────────────────────────────")
rows = conn.execute("""
    SELECT
        a.account_name,
        a.platform_account_id,
        COUNT(*)                 AS rows,
        MIN(t.date)              AS min_date,
        MAX(t.date)              AS max_date,
        SUM(t.cost_micros) / 1e6 AS spend,
        SUM(t.impressions)       AS impressions,
        SUM(t.clicks)            AS clicks
    FROM campaign_performance_hourly t
    JOIN accounts a ON t.account_id = a.id
    GROUP BY t.account_id
    ORDER BY a.platform_account_id
""").fetchall()

if not rows:
    print("  (no data yet — run: --hourly-days 90 --hourly-only)")
else:
    print(f"  {'Account':<20} {'Min date':<12} {'Max date':<12} "
          f"{'Rows':>8} {'Spend':>12} {'Impr':>12} {'Clicks':>8}")
    print(f"  {'─'*20} {'─'*12} {'─'*12} {'─'*8} {'─'*12} {'─'*12} {'─'*8}")
    for r in rows:
        print(
            f"  {r['account_name'][:19]:<20} "
            f"{str(r['min_date']):<12} "
            f"{str(r['max_date']):<12} "
            f"{r['rows']:>8,} "
            f"${r['spend']:>11,.0f} "
            f"{r['impressions']:>12,} "
            f"{r['clicks']:>8,}"
        )
print()

# ── campaign_performance_geo_daily ────────────────────────────────────────────
print("── campaign_performance_geo_daily ───────────────────────────────")
rows = conn.execute("""
    SELECT
        a.account_name,
        a.platform_account_id,
        COUNT(*)                 AS rows,
        MIN(t.date)              AS min_date,
        MAX(t.date)              AS max_date,
        SUM(t.cost_micros) / 1e6 AS spend,
        SUM(t.impressions)       AS impressions,
        SUM(t.clicks)            AS clicks
    FROM campaign_performance_geo_daily t
    JOIN accounts a ON t.account_id = a.id
    GROUP BY t.account_id
    ORDER BY a.platform_account_id
""").fetchall()

if not rows:
    print("  (no data yet — run: --geo-start 2025-01-01 --geo-only)")
else:
    print(f"  {'Account':<20} {'Min date':<12} {'Max date':<12} "
          f"{'Rows':>8} {'Spend':>12} {'Impr':>12} {'Clicks':>8}")
    print(f"  {'─'*20} {'─'*12} {'─'*12} {'─'*8} {'─'*12} {'─'*12} {'─'*8}")
    for r in rows:
        print(
            f"  {r['account_name'][:19]:<20} "
            f"{str(r['min_date']):<12} "
            f"{str(r['max_date']):<12} "
            f"{r['rows']:>8,} "
            f"${r['spend']:>11,.0f} "
            f"{r['impressions']:>12,} "
            f"{r['clicks']:>8,}"
        )
print()

# ── keyword_performance_daily ─────────────────────────────────────────────────
print("── keyword_performance_daily  (device='ALL', geo='ALL') ─────────")
rows = conn.execute("""
    SELECT
        a.account_name,
        a.platform_account_id,
        COUNT(*)                        AS rows,
        MIN(kpd.report_date)            AS min_date,
        MAX(kpd.report_date)            AS max_date,
        SUM(kpd.cost_micros) / 1e6      AS spend,
        SUM(kpd.impressions)            AS impressions,
        SUM(kpd.clicks)                 AS clicks,
        SUM(kpd.primary_conv_value)     AS revenue
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON ki.id = kpd.instance_id
    JOIN accounts a           ON a.id  = ki.account_id
    WHERE kpd.device    = 'ALL'
      AND kpd.geo_state = 'ALL'
    GROUP BY ki.account_id
    ORDER BY a.platform_account_id
""").fetchall()

if not rows:
    print("  (no data yet — run kw_db_scripts sync next)")
else:
    print(f"  {'Account':<20} {'Min date':<12} {'Max date':<12} "
          f"{'Rows':>7} {'Spend':>12} {'Impr':>12} {'Clicks':>8} {'Revenue':>10}")
    print(f"  {'─'*20} {'─'*12} {'─'*12} "
          f"{'─'*7} {'─'*12} {'─'*12} {'─'*8} {'─'*10}")
    for r in rows:
        print(
            f"  {r['account_name'][:19]:<20} "
            f"{str(r['min_date']):<12} "
            f"{str(r['max_date']):<12} "
            f"{r['rows']:>7,} "
            f"${r['spend']:>11,.0f} "
            f"{r['impressions']:>12,} "
            f"{r['clicks']:>8,} "
            f"${r['revenue']:>9,.0f}"
        )
print()

# ── search_query_performance ──────────────────────────────────────────────────
print("── search_query_performance  (device='ALL') ─────────────────────")
rows = conn.execute("""
    SELECT
        a.account_name,
        a.platform_account_id,
        COUNT(*)                        AS rows,
        COUNT(DISTINCT sqp.query_id)    AS unique_queries,
        MIN(sqp.report_date)            AS min_date,
        MAX(sqp.report_date)            AS max_date,
        SUM(sqp.cost_micros) / 1e6      AS spend,
        SUM(sqp.clicks)                 AS clicks
    FROM search_query_performance sqp
    JOIN keyword_instances ki ON ki.id = sqp.instance_id
    JOIN accounts a           ON a.id  = ki.account_id
    WHERE sqp.device = 'ALL'
    GROUP BY ki.account_id
    ORDER BY a.platform_account_id
""").fetchall()

if not rows:
    print("  (no data yet — run kw_db_scripts sync next)")
else:
    print(f"  {'Account':<20} {'Queries':>8} {'Min date':<12} {'Max date':<12} "
          f"{'Rows':>8} {'Spend':>12} {'Clicks':>8}")
    print(f"  {'─'*20} {'─'*8} {'─'*12} {'─'*12} {'─'*8} {'─'*12} {'─'*8}")
    for r in rows:
        print(
            f"  {r['account_name'][:19]:<20} "
            f"{r['unique_queries']:>8,} "
            f"{str(r['min_date']):<12} "
            f"{str(r['max_date']):<12} "
            f"{r['rows']:>8,} "
            f"${r['spend']:>11,.0f} "
            f"{r['clicks']:>8,}"
        )
print()

# ── account_id type confirmation ──────────────────────────────────────────────
print("── account_id type check ────────────────────────────────────────")
for table in ("campaigns", "campaign_performance_daily"):
    sample = conn.execute(
        f"SELECT campaign_id, account_id, typeof(account_id) AS id_type "
        f"FROM {table} LIMIT 2"
    ).fetchall()
    if sample:
        for r in sample:
            print(f"  [{table}] campaign_id={r['campaign_id']}  "
                  f"account_id={r['account_id']}  type={r['id_type']}")
    else:
        print(f"  [{table}] (empty)")

conn.close()
print("\nDone.")
