#!/usr/bin/env python
"""
sync_ai_max_flags.py -- v2. Syncs AI Max settings at both grains:
  campaigns.ai_max_enabled   (0/1)   <- campaign.ai_max_setting.enable_ai_max
  campaigns.ai_max_bundling  (TEXT)  <- campaign.ai_max_setting.bundling_required (enum name)
  ad_groups.<key>.ai_max_stm_disabled (0/1) <- ad_group.ai_max_ad_group_setting.disable_search_term_matching

v2 fixes: ad_groups key column detected at runtime via PRAGMA (v1 guessed 'id'
and crashed); account scoping applied if ad_groups carries account_id; single
commit at the end. Field names verified against live v23 metadata (9/2).
The ai_max_search_term_ad_combination_view resource is deliberately NOT synced
here -- it's served-traffic reporting, not a setting (future analysis pull).

    python sync_ai_max_flags.py                (dry run)
    python sync_ai_max_flags.py --apply
"""
import argparse
import sqlite3

from dotenv import load_dotenv
load_dotenv()
from google.ads.googleads.client import GoogleAdsClient

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
CUSTOMER_ID, ACCOUNT_ID = "8886502147", 1

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()

client = GoogleAdsClient.load_from_storage()
ga = client.get_service("GoogleAdsService")

camp_rows = []
for batch in ga.search_stream(customer_id=CUSTOMER_ID, query="""
        SELECT campaign.id, campaign.name,
               campaign.ai_max_setting.enable_ai_max,
               campaign.ai_max_setting.bundling_required
        FROM campaign WHERE campaign.status != 'REMOVED'"""):
    for r in batch.results:
        camp_rows.append((str(r.campaign.id), r.campaign.name,
                          1 if r.campaign.ai_max_setting.enable_ai_max else 0,
                          r.campaign.ai_max_setting.bundling_required.name))

ag_rows = []
for batch in ga.search_stream(customer_id=CUSTOMER_ID, query="""
        SELECT ad_group.id, ad_group.ai_max_ad_group_setting.disable_search_term_matching
        FROM ad_group WHERE ad_group.status != 'REMOVED'
          AND campaign.status != 'REMOVED'"""):
    for r in batch.results:
        ag_rows.append((str(r.ad_group.id),
                        1 if r.ad_group.ai_max_ad_group_setting.disable_search_term_matching else 0))

on = [x for x in camp_rows if x[2] == 1]
print("campaigns fetched: %d | AI Max ON: %d" % (len(camp_rows), len(on)))
for cid, name, _, bundling in sorted(on, key=lambda x: x[1]):
    print("   ON  %s  bundling=%-12s %s" % (cid, bundling, name[:52]))
print("ad groups fetched: %d | search-term-matching disabled: %d"
      % (len(ag_rows), sum(x[1] for x in ag_rows)))

conn = sqlite3.connect(DB)
ag_cols = [r[1] for r in conn.execute("PRAGMA table_info(ad_groups)")]
ag_key = next((c for c in ("ad_group_id", "id") if c in ag_cols), None)
ag_has_acct = "account_id" in ag_cols
print("ad_groups key column detected: %s | account_id present: %s" % (ag_key, ag_has_acct))
if ag_key is None:
    print("[ABORT] ad_groups has neither ad_group_id nor id -- columns are: %s" % ag_cols)
    raise SystemExit(1)

if not args.apply:
    print("[DRY RUN] no DB changes. Re-run with --apply to write.")
    raise SystemExit(0)

for table, col, typ in (("campaigns", "ai_max_enabled", "INTEGER"),
                        ("campaigns", "ai_max_bundling", "TEXT"),
                        ("ad_groups", "ai_max_stm_disabled", "INTEGER")):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    if col not in cols:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))
        print("[MIGRATE] added %s.%s" % (table, col))

n1 = 0
for cid, _, flag, bundling in camp_rows:
    n1 += conn.execute(
        "UPDATE campaigns SET ai_max_enabled = ?, ai_max_bundling = ? "
        "WHERE account_id = ? AND campaign_id = ?",
        (flag, bundling, ACCOUNT_ID, cid)).rowcount

ag_sql = "UPDATE ad_groups SET ai_max_stm_disabled = ? WHERE %s = ?" % ag_key
if ag_has_acct:
    ag_sql += " AND account_id = %d" % ACCOUNT_ID
n2 = 0
for agid, flag in ag_rows:
    n2 += conn.execute(ag_sql, (flag, agid)).rowcount
conn.commit()
print("[APPLY] campaigns updated: %d | ad_groups updated: %d" % (n1, n2))
print("verify campaigns:", conn.execute(
    "SELECT ai_max_enabled, ai_max_bundling, COUNT(*) FROM campaigns "
    "WHERE account_id = 1 GROUP BY 1, 2").fetchall())
conn.close()
