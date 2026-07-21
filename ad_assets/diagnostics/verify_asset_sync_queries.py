#!/usr/bin/env python
"""
verify_asset_sync_queries.py -- the three post-acceptance checks in one run. READ-ONLY.

  1. link_status of the 150 zero-impression AD_GROUP associations
  2. 30-day probe-key ground-truth (asset 317584962874 on campaign 23385188622)
  3. CA-sitelink verdict: YTD click_type split on the Tier2 shell (23530860942)

    python verify_asset_sync_queries.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"

conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
c = conn.cursor()

print("=" * 68)
print("1) AD_GROUP association link_status (the 150 zero-impression links)")
print("=" * 68)
for r in c.execute("""
    SELECT link_status, COUNT(*) FROM asset_associations
    WHERE account_id = 1 AND association_level = 'AD_GROUP'
    GROUP BY link_status"""):
    print("  %-12s %d" % (r[0], r[1]))

print("\n" + "=" * 68)
print("2) 30-day probe-key ground truth (expect ~4,632 / 413 / 104.5)")
print("=" * 68)
r = c.execute("""
    SELECT SUM(impressions), SUM(clicks), SUM(conversions), SUM(conversions_value)
    FROM asset_performance_daily
    WHERE asset_id = 317584962874 AND association_id = 23385188622
      AND device = 'ALL' AND click_type = 'ALL'
      AND report_date BETWEEN date('now','-31 days') AND date('now','-1 day')
""").fetchone()
print("  impr %s | clicks %s | conv %.1f | value %.2f"
      % (r[0], r[1], r[2] or 0, r[3] or 0))

print("\n" + "=" * 68)
print("3) CA-sitelink verdict: YTD by click_type on Tier2 shell 23530860942")
print("   (SITELINKS row = the sitelink's own contribution)")
print("=" * 68)
for r in c.execute("""
    SELECT click_type, SUM(impressions), SUM(clicks), SUM(conversions), SUM(conversions_value)
    FROM asset_performance_daily
    WHERE asset_id = 317584962874 AND association_id = 23530860942
      AND device = 'ALL' AND click_type != 'ALL'
    GROUP BY click_type"""):
    print("  %-12s impr %8s | clicks %6s | conv %8.1f | value %10.2f"
          % (r[0], r[1], r[2], r[3] or 0, r[4] or 0))

conn.close()
print("\nDone. Paste this output back to the threads.")
