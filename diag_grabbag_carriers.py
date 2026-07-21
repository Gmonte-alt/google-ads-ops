"""Read-only: confirm the grab-bag carrier keywords are hierarchy_enabled (so the pause finds them),
and show why is_serving read 0. Run: python diag_grabbag_carriers.py
"""
import sqlite3
DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
print("Grab-bag (Home_Group*) Kin/Costco/American Family keywords in ENABLED campaigns,")
print("by ad_group_status / hierarchy_enabled / is_serving / keyword_status:")
for r in c.execute(
    "SELECT ad_group_name, ad_group_status, hierarchy_enabled, is_serving, keyword_status, COUNT(*) "
    "FROM keyword_current WHERE account_id=1 AND campaign_status='ENABLED' "
    "AND lower(ad_group_name) LIKE '%home_group%' "
    "AND (lower(keyword_text) LIKE 'kin %' OR lower(keyword_text) LIKE '%costco%' "
    "     OR lower(keyword_text) LIKE '%american family%') "
    "GROUP BY 1,2,3,4,5 ORDER BY 6 DESC LIMIT 20"):
    print("  ", tuple(r))
c.close()
