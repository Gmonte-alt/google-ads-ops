#!/usr/bin/env python
"""
apply_search_only_views.py -- campaign_db_scripts\ -- creates the three
Search-only views for the PMax era. PURELY ADDITIVE (CREATE VIEW IF NOT
EXISTS); no tables touched, no data modified. Consumers that must stay
Search-only swap their FROM to the *_search view; consumers reconciling to
account totals keep the base tables.

    python apply_search_only_views.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"          (dry run)
    python apply_search_only_views.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db" --apply
"""
import argparse
import sqlite3

VIEWS = {
    "campaigns_search": """
        CREATE VIEW IF NOT EXISTS campaigns_search AS
        SELECT * FROM campaigns
        WHERE campaign_type = 'SEARCH'
           OR campaign_type IS NULL  -- pre-PMax rows synced before the column was populated
    """,
    "campaign_performance_daily_search": """
        CREATE VIEW IF NOT EXISTS campaign_performance_daily_search AS
        SELECT p.* FROM campaign_performance_daily p
        JOIN campaigns c ON c.campaign_id = p.campaign_id AND c.account_id = p.account_id
        WHERE c.campaign_type = 'SEARCH' OR c.campaign_type IS NULL
    """,
    "campaign_performance_geo_daily_search": """
        CREATE VIEW IF NOT EXISTS campaign_performance_geo_daily_search AS
        SELECT p.* FROM campaign_performance_geo_daily p
        JOIN campaigns c ON c.campaign_id = p.campaign_id AND c.account_id = p.account_id
        WHERE c.campaign_type = 'SEARCH' OR c.campaign_type IS NULL
    """,
}

ap = argparse.ArgumentParser()
ap.add_argument("--db", required=True)
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()

conn = sqlite3.connect(args.db)
existing = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='view'")}
print("[PLAN]")
for name in VIEWS:
    print("  view %-42s %s" % (name, "exists (skip)" if name in existing else "WILL CREATE"))

# sanity: is the channel column populated?
dist = conn.execute(
    "SELECT campaign_type, COUNT(*) FROM campaigns "
    "WHERE account_id = 1 GROUP BY 1").fetchall()
print("[INFO] account-1 campaigns by channel type:", dist)

if not args.apply:
    print("[DRY RUN] no changes made.")
else:
    for name, ddl in VIEWS.items():
        conn.executescript(ddl)
    conn.commit()
    for name in VIEWS:
        n = conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]
        print("[VERIFY] %-42s %s rows" % (name, format(n, ",")))
    print("[APPLY] done. Search-only consumers swap FROM to the _search views.")
conn.close()
