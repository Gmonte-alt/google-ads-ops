#!/usr/bin/env python
"""
probe_brand_registry_dki.py -- READ-ONLY. Determines where (or whether) the
no-DKI designation lives in brand_registry: restriction_reason vocabulary,
match_risk vocabulary, and any free-text DKI mentions.

    python probe_brand_registry_dki.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)

print("1) restriction_reason vocabulary:")
for r in c.execute("SELECT restriction_reason, COUNT(*) FROM brand_registry "
                   "GROUP BY 1 ORDER BY 2 DESC"):
    print("   %-50s %s" % (str(r[0])[:50], r[1]))

print("\n2) match_risk vocabulary:")
for r in c.execute("SELECT match_risk, COUNT(*) FROM brand_registry GROUP BY 1"):
    print("   %-30s %s" % (str(r[0])[:30], r[1]))

print("\n3) rows mentioning DKI anywhere (notes/reason/risk):")
q = ("SELECT brand_name, bid_status, restriction_reason, substr(notes,1,60) "
     "FROM brand_registry WHERE LOWER(COALESCE(notes,'') || "
     "COALESCE(restriction_reason,'') || COALESCE(match_risk,'')) LIKE '%dki%'")
rows = c.execute(q).fetchall()
if not rows:
    print("   (none -- no-DKI is NOT encoded in the registry's text fields)")
for r in rows:
    print("   ", r)

print("\n4) restriction_source vocabulary (who imposes restrictions):")
for r in c.execute("SELECT restriction_source, COUNT(*) FROM brand_registry "
                   "GROUP BY 1 ORDER BY 2 DESC"):
    print("   %-40s %s" % (str(r[0])[:40], r[1]))
c.close()
