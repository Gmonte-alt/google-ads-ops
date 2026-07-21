"""Read-only diagnostic: why does GROUP BY criterion_id over-collapse keyword_instances?
Run: python diag_keyword_instances.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)


def one(sql, *a):
    return c.execute(sql, a).fetchone()


def many(sql, *a):
    return c.execute(sql, a).fetchall()


print("=" * 70)
rows = one("SELECT COUNT(*) FROM keyword_instances WHERE account_id=1")[0]
d_crit = one("SELECT COUNT(DISTINCT criterion_id) FROM keyword_instances WHERE account_id=1")[0]
d_ag_crit = one("SELECT COUNT(*) FROM (SELECT 1 FROM keyword_instances WHERE account_id=1 "
                "GROUP BY ad_group_id, criterion_id)")[0]
d_camp_ag_crit = one("SELECT COUNT(*) FROM (SELECT 1 FROM keyword_instances WHERE account_id=1 "
                     "GROUP BY campaign_id, ad_group_id, criterion_id)")[0]
print("raw rows                         :", rows)
print("distinct criterion_id            :", d_crit)
print("distinct (ad_group_id,criterion) :", d_ag_crit)
print("distinct (camp,ad_group,criterion):", d_camp_ag_crit)
print()
print(">> If distinct criterion_id << distinct (ad_group_id,criterion_id),")
print("   criterion_id is SHARED across ad groups -> GROUP BY criterion_id collapses them.")
print("=" * 70)

print("\n-- FORMAT CHECK (brackets?) --")
print("keyword_instances.campaign_id / ad_group_id (first 3):")
for r in many("SELECT campaign_id, ad_group_id FROM keyword_instances WHERE account_id=1 LIMIT 3"):
    print("   ", repr(r[0]), repr(r[1]))
print("campaigns.campaign_id (first 3):")
for r in many("SELECT campaign_id FROM campaigns WHERE account_id=1 LIMIT 3"):
    print("   ", repr(r[0]))
print("ad_groups.ad_group_id (first 3):")
for r in many("SELECT ad_group_id FROM ad_groups WHERE account_id=1 LIMIT 3"):
    print("   ", repr(r[0]))
print(">> If ki.campaign_id looks like '[123]' but campaigns.campaign_id is '123', the direct")
print("   campaign_id join fails -> most rows dropped.")

print("\n-- DOES ONE criterion_id SPAN MANY AD GROUPS / CAMPAIGNS? --")
for cid, nag, ncamp, nrows in many(
        "SELECT criterion_id, COUNT(DISTINCT ad_group_id), COUNT(DISTINCT campaign_id), COUNT(*) "
        "FROM keyword_instances WHERE account_id=1 GROUP BY criterion_id "
        "ORDER BY COUNT(DISTINCT ad_group_id) DESC LIMIT 5"):
    print("   criterion_id=%s : %s ad_groups, %s campaigns, %s rows" % (cid, nag, ncamp, nrows))
print(">> n_ad_groups > 1 confirms criterion_id is shared (not unique per instance).")

print("\n-- SAMPLE: all rows for one shared criterion_id (see how match_type/ad_group vary) --")
top = one("SELECT criterion_id FROM keyword_instances WHERE account_id=1 "
          "GROUP BY criterion_id ORDER BY COUNT(DISTINCT ad_group_id) DESC LIMIT 1")
if top:
    for r in many("SELECT id, keyword_text, match_type, campaign_id, ad_group_id, status "
                  "FROM keyword_instances WHERE account_id=1 AND criterion_id=? "
                  "ORDER BY id DESC LIMIT 8", top[0]):
        print("   id=%s | %s | %s | camp=%s | ag=%s | status=%s" % r)
c.close()
