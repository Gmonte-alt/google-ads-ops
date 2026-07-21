import sqlite3
import carrier_enrich as CE

# synthetic brand_registry with the REAL column set
c = sqlite3.connect(":memory:")
c.execute("""CREATE TABLE brand_registry (id INTEGER PRIMARY KEY, brand_name TEXT, normalized_name TEXT,
  parent_brand TEXT, bid_status TEXT, restriction_reason TEXT, restriction_source TEXT, effective_date TEXT,
  verticals TEXT, notes TEXT, source_sheet TEXT, brand_category TEXT, match_risk TEXT)""")
c.execute("CREATE TABLE brand_registry_keywords (id INTEGER PRIMARY KEY, brand_id INT, keyword_text TEXT, normalized TEXT, match_type TEXT, source_sheet TEXT)")
brands = [  # id, name, verticals, brand_category
 (1,"GEICO","auto,home","carrier_national"),
 (2,"Travelers","home,auto","carrier_national"),
 (3,"Lemonade","home,renters","carrier_insurtech"),
 (4,"NerdWallet","home,auto,life","aggregator_competitor"),
 (5,"Humana","medicare","carrier_health"),          # carrier but NOT home line-of-business
]
for bid,nm,verts,cat in brands:
    c.execute("INSERT INTO brand_registry (id,brand_name,verticals,brand_category) VALUES (?,?,?,?)",(bid,nm,verts,cat))
kws = [(1,"geico"),(1,"geico Home and Auto"),(2,"travelers insurance"),(3,"home insurance lemonade"),
       (4,"nerdwallet home insurance"),(5,"humana medicare")]
for bid,kw in kws:
    c.execute("INSERT INTO brand_registry_keywords (brand_id,keyword_text) VALUES (?,?)",(bid,kw))
c.commit()
lut = CE.build_brand_lookup(c)
print("brand lookup entries:", len(lut))

print("\n=== extract_keywords on real keyword_add prose ===")
samples = [
 '2 broad match keywords added\n  geico\n  geico: Label is "Geico"',
 '1 exact match keyword added\n  [geico Home and Auto]',
 '2 exact match keywords added\n  [utah house insurance]\n  [south dakota home insurance]',
 '2 phrase match keywords added\n  "travelers auto and home insurance"\n  "farmers auto and home insurance"',
 '4 exact match keywords paused\n  [home insurance lemonade]: Status changed',
]
for s in samples:
    kws_out = CE.extract_keywords(s)
    print("  %-45s -> %s" % (s.split(chr(10))[0], kws_out))

print("\n=== classify (carrier | carrier_other | competitor | branded_other | generic) ===")
for raw in ["geico","[geico Home and Auto]","travelers insurance","home insurance lemonade",
            "nerdwallet home insurance","humana medicare","utah house insurance"]:
    n = CE.normalize_kw(raw)
    lab = CE.classify(n, lut)
    print("  %-32s -> %-14s (mgmt 3way: %s | binary: %s)" % (
        raw, lab, CE.to_bucket(lab, True), CE.to_bucket(lab, False)))
c.close()
