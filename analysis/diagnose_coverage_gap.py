"""
diagnose_coverage_gap.py
------------------------
Diagnostic script to identify why Microsoft keywords are not appearing
in the coverage gap analysis. Run this and paste the output back.

Usage:
    python analysis/diagnose_coverage_gap.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=" * 60)
print("COVERAGE GAP DIAGNOSTIC")
print("=" * 60)

# ------------------------------------------------------------------
# Check 1: Account IDs in the database
# ------------------------------------------------------------------
print("\n[1] Accounts in database:")
rows = cur.execute("""
    SELECT id, account_name, platform_account_id, platform
    FROM accounts ORDER BY platform, account_name
""").fetchall()
for r in rows:
    print(f"  id={r['id']}  {r['platform']:<12}  {r['platform_account_id']:<12}  {r['account_name']}")

# ------------------------------------------------------------------
# Check 2: Keyword instance counts per account
# ------------------------------------------------------------------
print("\n[2] Keyword instances per account:")
rows = cur.execute("""
    SELECT a.account_name, a.platform, a.platform_account_id,
           COUNT(*) as instance_count
    FROM keyword_instances ki
    JOIN accounts a ON ki.account_id = a.id
    GROUP BY a.id
""").fetchall()
for r in rows:
    print(f"  {r['platform']:<12}  {r['platform_account_id']:<12}  "
          f"{r['account_name']:<20}  {r['instance_count']:,} instances")

# ------------------------------------------------------------------
# Check 3: Performance rows per account
# ------------------------------------------------------------------
print("\n[3] keyword_performance_daily rows per account:")
rows = cur.execute("""
    SELECT a.account_name, a.platform, a.platform_account_id,
           COUNT(*) as perf_rows,
           MIN(kpd.report_date) as earliest,
           MAX(kpd.report_date) as latest,
           SUM(kpd.clicks) as total_clicks,
           SUM(kpd.impressions) as total_impressions
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE kpd.device = 'ALL' AND kpd.geo_state = 'ALL'
    GROUP BY a.id
""").fetchall()
for r in rows:
    print(f"  {r['platform']:<12}  {r['platform_account_id']:<12}  "
          f"{r['account_name']:<20}  {r['perf_rows']:,} rows  "
          f"{r['earliest']} to {r['latest']}  "
          f"clicks={r['total_clicks']:,}  impressions={r['total_impressions']:,}")

# ------------------------------------------------------------------
# Check 4: Sample Microsoft identity keys
# ------------------------------------------------------------------
print("\n[4] Sample Microsoft keyword_identities (first 10):")
rows = cur.execute("""
    SELECT DISTINCT kd.identity_key, kd.match_type
    FROM keyword_instances ki
    JOIN accounts a ON ki.account_id = a.id
    JOIN keyword_identities kd ON ki.identity_id = kd.id
    WHERE a.platform = 'microsoft'
    LIMIT 10
""").fetchall()
for r in rows:
    print(f"  {r['identity_key']}")

# ------------------------------------------------------------------
# Check 5: Sample Google identity keys
# ------------------------------------------------------------------
print("\n[5] Sample Google keyword_identities (first 10):")
rows = cur.execute("""
    SELECT DISTINCT kd.identity_key, kd.match_type
    FROM keyword_instances ki
    JOIN accounts a ON ki.account_id = a.id
    JOIN keyword_identities kd ON ki.identity_id = kd.id
    WHERE a.platform = 'google'
      AND a.platform_account_id = '8886502147'
    LIMIT 10
""").fetchall()
for r in rows:
    print(f"  {r['identity_key']}")

# ------------------------------------------------------------------
# Check 6: Direct overlap test -- do any identity keys appear on both?
# ------------------------------------------------------------------
print("\n[6] Direct identity_key overlap count:")
row = cur.execute("""
    SELECT COUNT(DISTINCT ki_ms.identity_id) as overlap
    FROM keyword_instances ki_ms
    JOIN accounts a_ms ON ki_ms.account_id = a_ms.id
    JOIN keyword_instances ki_g ON ki_ms.identity_id = ki_g.identity_id
    JOIN accounts a_g ON ki_g.account_id = a_g.id
    WHERE a_ms.platform = 'microsoft'
      AND a_g.platform  = 'google'
      AND a_g.platform_account_id = '8886502147'
""").fetchone()
print(f"  Keywords on both platforms: {row['overlap']:,}")

# ------------------------------------------------------------------
# Check 7: Are Microsoft instances linked to identities at all?
# ------------------------------------------------------------------
print("\n[7] Microsoft instances without an identity_id:")
row = cur.execute("""
    SELECT COUNT(*) as n
    FROM keyword_instances ki
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform = 'microsoft'
      AND ki.identity_id IS NULL
""").fetchone()
print(f"  Instances with NULL identity_id: {row['n']:,}")

# ------------------------------------------------------------------
# Check 8: Sample raw Microsoft criterion_id and keyword_text
# ------------------------------------------------------------------
print("\n[8] Sample Microsoft keyword_instances (first 5):")
rows = cur.execute("""
    SELECT ki.criterion_id, ki.keyword_text, ki.match_type,
           kd.identity_key, kd.normalized_text
    FROM keyword_instances ki
    JOIN accounts a ON ki.account_id = a.id
    JOIN keyword_identities kd ON ki.identity_id = kd.id
    WHERE a.platform = 'microsoft'
    LIMIT 5
""").fetchall()
for r in rows:
    print(f"  criterion={r['criterion_id']}  text='{r['keyword_text']}'  "
          f"match={r['match_type']}")
    print(f"    identity_key='{r['identity_key']}'  "
          f"normalized='{r['normalized_text']}'")

# ------------------------------------------------------------------
# Check 9: Microsoft performance in the 90-day analysis window
# ------------------------------------------------------------------
print("\n[9] Microsoft performance rows in last 90 days:")
row = cur.execute("""
    SELECT COUNT(*) as n,
           SUM(kpd.clicks) as clicks,
           SUM(kpd.impressions) as impressions
    FROM keyword_performance_daily kpd
    JOIN keyword_instances ki ON kpd.instance_id = ki.id
    JOIN accounts a ON ki.account_id = a.id
    WHERE a.platform = 'microsoft'
      AND kpd.device = 'ALL'
      AND kpd.geo_state = 'ALL'
      AND kpd.report_date BETWEEN '2025-12-23' AND '2026-03-23'
""").fetchone()
print(f"  Rows: {row['n']:,}  Clicks: {row['clicks']}  "
      f"Impressions: {row['impressions']}")

conn.close()
print("\n" + "=" * 60)
print("Paste the full output above back into the conversation.")
print("=" * 60)
