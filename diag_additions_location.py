"""Read-only: where did the carrier additions actually land, and what are the active campaigns?
Run: python diag_additions_location.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)

print("=== ENABLED (active) campaigns and their keyword counts ===")
n = 0
for name, cnt in c.execute(
        "SELECT campaign_name, COUNT(*) FROM keyword_current WHERE account_id=1 "
        "AND campaign_status='ENABLED' GROUP BY campaign_name ORDER BY 2 DESC"):
    print("   %-58s %d" % (name, cnt))
    n += 1
print("   (%d enabled campaigns total)" % n)

print("\n=== where the new '-Core-New' carrier groups live, by campaign status ===")
for status, ncamp, nkw in c.execute(
        "SELECT campaign_status, COUNT(DISTINCT campaign_name), COUNT(*) FROM keyword_current "
        "WHERE account_id=1 AND lower(ad_group_name) LIKE '%core-new%' GROUP BY campaign_status"):
    print("   status=%-8s : %d campaigns, %d keyword rows" % (status, ncamp, nkw))

print("\n=== are the new carrier groups in ANY enabled campaign? ===")
hit = c.execute(
    "SELECT COUNT(*) FROM keyword_current WHERE account_id=1 AND campaign_status='ENABLED' "
    "AND lower(ad_group_name) LIKE '%core-new%'").fetchone()[0]
print("   new carrier-group keywords in ENABLED campaigns: %d" % hit)

print("\n=== sample campaigns holding Costco-Core-New (name + status) ===")
for name, status in c.execute(
        "SELECT DISTINCT campaign_name, campaign_status FROM keyword_current WHERE account_id=1 "
        "AND lower(ad_group_name) LIKE '%costco%core%new%' ORDER BY campaign_status LIMIT 15"):
    print("   %-58s %s" % (name, status))
c.close()
