"""Read-only: confirm the status columns in keyword_current, to explain why campaign_status='ENABLED'
matched nothing and verify hierarchy_enabled is populated. Run: python diag_keyword_current.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)

print("campaign_status distinct values (acct 1):")
for v, n in c.execute("SELECT campaign_status, COUNT(*) FROM keyword_current WHERE account_id=1 "
                      "GROUP BY campaign_status ORDER BY 2 DESC"):
    print("   %r : %d" % (v, n))

print("\nhierarchy_enabled distinct values (acct 1):")
for v, n in c.execute("SELECT hierarchy_enabled, COUNT(*) FROM keyword_current WHERE account_id=1 "
                      "GROUP BY hierarchy_enabled ORDER BY 2 DESC"):
    print("   %r : %d" % (v, n))

print("\ncosto condo insurance across its costco-core ad groups (should show the wrong BROAD in a live group):")
for r in c.execute(
        "SELECT ad_group_name, campaign_status, hierarchy_enabled, match_type, COUNT(*) "
        "FROM keyword_current WHERE account_id=1 AND lower(keyword_text)='costco condo insurance' "
        "AND lower(ad_group_name) LIKE '%costco%core%' GROUP BY 1,2,3,4 ORDER BY 5 DESC LIMIT 10"):
    print("   ", tuple(r))
c.close()
