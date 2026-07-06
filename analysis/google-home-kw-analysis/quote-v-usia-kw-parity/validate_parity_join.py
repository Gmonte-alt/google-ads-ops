#!/usr/bin/env python
"""Synthetic validation for the three-tier parity_join. Builds a fixture with campaigns/ad_groups,
runs the join, asserts covering tiers / navigate / GENERIC_HOME / bid_status / gap, then the caller
recalcs the xlsx for zero formula errors."""
import sys, types, sqlite3, os, tempfile

for name in ["sklearn", "sklearn.feature_extraction", "sklearn.feature_extraction.text",
             "sklearn.metrics", "sklearn.metrics.pairwise"]:
    sys.modules[name] = types.ModuleType(name)
sys.modules["sklearn.feature_extraction.text"].TfidfVectorizer = object
sys.modules["sklearn.metrics.pairwise"].cosine_similarity = lambda *a, **k: None

import parity_join as pj
from parity_common import categorize

DB = os.path.join(tempfile.gettempdir(), "parity_join_fx.db")
OUT = os.path.join(tempfile.gettempdir(), "parity_join_out2")
for p in (DB,):
    if os.path.exists(p): os.remove(p)
c = sqlite3.connect(DB); cur = c.cursor()
cur.executescript("""
CREATE TABLE search_queries (id INTEGER PRIMARY KEY, query_text TEXT, normalized_text TEXT,
    theme TEXT, modifier_brand TEXT, target_theme TEXT, target_group TEXT);
CREATE TABLE keyword_instances (id INTEGER PRIMARY KEY, account_id INTEGER, keyword_text TEXT,
    match_type TEXT, campaign_id TEXT, ad_group_id TEXT, campaign_name TEXT, ad_group_name TEXT, status TEXT);
CREATE TABLE campaigns (campaign_id TEXT, account_id INTEGER, campaign_name TEXT, status TEXT);
CREATE TABLE ad_groups (ad_group_id TEXT, campaign_id TEXT, account_id INTEGER, ad_group_name TEXT, status TEXT);
CREATE TABLE search_query_performance (id INTEGER PRIMARY KEY, query_id INTEGER, instance_id INTEGER,
    device TEXT, report_date TEXT, impressions INTEGER, clicks INTEGER, cost_micros REAL,
    primary_conv_count REAL, primary_conv_value REAL);
CREATE TABLE brand_registry (id INTEGER PRIMARY KEY, brand_name TEXT, normalized_name TEXT,
    bid_status TEXT, brand_category TEXT, verticals TEXT);
CREATE TABLE brand_registry_keywords (id INTEGER PRIMARY KEY, brand_id INTEGER, normalized TEXT);
""")
# campaigns: C1 live, C2 paused
cur.executemany("INSERT INTO campaigns VALUES (?,?,?,?)", [
    ("C1", 1, "Home-Live", "ENABLED"), ("C2", 1, "Home-Conquest", "PAUSED")])
cur.executemany("INSERT INTO ad_groups VALUES (?,?,?,?,?)", [
    ("A1", "C1", 1, "generic", "ENABLED"), ("A2", "C2", 1, "kin", "ENABLED")])
# instances: home insurance (serving), cheap home insurance (serving), kin insurance (campaign paused)
cur.executemany("INSERT INTO keyword_instances VALUES (?,?,?,?,?,?,?,?,?)", [
    (1, 1, "home insurance", "EXACT", "C1", "A1", "Home-Live", "generic", "ENABLED"),
    (2, 1, "cheap home insurance", "PHRASE", "C1", "A1", "Home-Live", "generic", "ENABLED"),
    (3, 1, "kin insurance", "EXACT", "C2", "A2", "Home-Conquest", "kin", "ENABLED"),  # campaign PAUSED
    (9, 2, "renters insurance", "EXACT", "CX", "AX", "x", "x", "ENABLED"),           # account 2 -> ignored
])
sq = [
    (1, "home insurance quote", "home insurance quote", "OLD08", "", "QUOTE", "NONBRAND"),
    (2, "kin insurance", "kin insurance", "OLD08", "kin", "CORE", "BRAND"),
    (3, "home insurance", "home insurance", "OLD08", "", "GENERIC_HOME", "NONBRAND"),
    (4, "homeowners insurance login", "homeowners insurance login", "OLD08", "", "CORE", "NONBRAND"),
    (5, "cheap home insurance", "cheap home insurance", "OLD08", "", "PRICE", "NONBRAND"),
    (6, "geico home insurance", "geico home insurance", "OLD08", "geico", "GENERIC_HOME", "BRAND"),
]
cur.executemany("INSERT INTO search_queries VALUES (?,?,?,?,?,?,?)", sq)
perf = [  # (query_id, instance_id, imp, clicks, cost_micros, conv, value)
    (1, 1, 1000, 100, 300e6, 10, 5000),   # add_new nonbrand
    (2, 1, 500, 45, 200e6, 8, 3000),      # reenable brand (kin)
    (3, 1, 2000, 200, 400e6, 20, 10000),  # covered -> dropped
    (4, 1, 300, 30, 90e6, 3, 1500),       # navigate -> dropped
    (5, 1, 800, 50, 100e6, 5, 2500),      # covered -> dropped
    (6, 1, 500, 40, 200e6, 4, 3000),      # add_new brand (geico, restricted)
]
cur.executemany(
    "INSERT INTO search_query_performance (id,query_id,instance_id,device,report_date,impressions,"
    "clicks,cost_micros,primary_conv_count,primary_conv_value) VALUES (?,?,?,'ALL',date('now','-5 day'),?,?,?,?,?)",
    [(i+1, q, inst, imp, clk, cost, cv, val) for i,(q,inst,imp,clk,cost,cv,val) in enumerate(perf)])
cur.executemany("INSERT INTO brand_registry VALUES (?,?,?,?,?,?)", [
    (1, "Kin Insurance", "kin insurance", "restricted", "carrier", "home"),
    (2, "Geico", "geico", "restricted", "carrier_restricted_partner", "home"),
    (3, "Unattributed Negative Keyword", "unattributed_negative_keyword", "restricted", "", "")])
cur.executemany("INSERT INTO brand_registry_keywords VALUES (?,?,?)", [
    (10, 1, "kin insurance"), (11, 1, "kin"), (12, 2, "geico"), (20, 3, "kin"), (21, 3, "")])
c.commit(); c.close()

# minimal USIA generic + carrier CSVs so we control gap assertions
gcsv = os.path.join(tempfile.gettempdir(), "usia_gen.csv")
with open(gcsv, "w", newline="", encoding="utf-8") as f:
    f.write("keyword_text,product_code,vertical,geo_name,avg_monthly_searches,vertical_match,brand,brand_bid_status,brand_category,dnb_flag\n")
    f.write("home insurance quote,,home,United States,50000,on_vertical,,,,0\n")
    f.write("home insurance by address,,home,United States,3000,on_vertical,,,,0\n")
bcsv = os.path.join(tempfile.gettempdir(), "usia_brand.csv")
with open(bcsv, "w", newline="", encoding="utf-8") as f:
    f.write("campaign,ad_group,brand,keyword,match_type,volume,cluster_members\n")
    f.write("USIA_HOME_BRAND_CARRIER,kin,Kin Insurance,kin insurance,exact,12000,kin\n")
    f.write("USIA_HOME_BRAND_CARRIER,geico,Geico,geico home insurance,phrase,8000,geico\n")

xlsx = pj.run(DB, gcsv, bcsv, OUT, days=90, min_clicks=20, min_conv=2, brand_db=DB, run_date="2026-06-30")

import csv as _csv
rd = lambda n: list(_csv.DictReader(open(os.path.join(OUT, n), encoding="utf-8")))
nb, br, gap = rd("adds_nonbrand.csv"), rd("adds_brand.csv"), rd("usia_target_gap.csv")
fails = 0
def check(label, cond):
    global fails; print("  [%s] %s" % ("PASS" if cond else "FAIL", label));  fails += (0 if cond else 1)

nbq = {r["query"]: r for r in nb}; brq = {r["query"]: r for r in br}
check("categorize('home insurance') == GENERIC_HOME", categorize("home insurance") == "GENERIC_HOME")
check("kin insurance is a BRAND reenable_existing", "kin insurance" in brq and brq["kin insurance"]["action"] == "reenable_existing")
check("kin reenable Where names the paused campaign", "kin insurance" in brq and "PAUSED" in brq["kin insurance"]["where"])
check("home insurance quote is add_new nonbrand", "home insurance quote" in nbq and nbq["home insurance quote"]["action"] == "add_new")
check("actively-covered 'home insurance' excluded from adds", "home insurance" not in nbq and "home insurance" not in brq)
check("covered 'cheap home insurance' excluded", "cheap home insurance" not in nbq)
check("navigate 'homeowners insurance login' suppressed", "homeowners insurance login" not in nbq and "homeowners insurance login" not in brq)
check("geico add carries bid_status=restricted", "geico home insurance" in brq and brq["geico home insurance"]["bid_status"] == "restricted")
gapk = {r["keyword"]: r for r in gap}
check("gap: 'kin insurance' coverage=paused (re-enable)", "kin insurance" in gapk and gapk["kin insurance"]["coverage"] == "paused")
check("gap: 'home insurance by address' coverage=absent", "home insurance by address" in gapk and gapk["home insurance by address"]["coverage"] == "absent")
check("gap excludes actively-covered USIA targets", all(r["coverage"] != "serving" for r in gap))
tm = rd("theme_underbuilt_map.csv")
check("theme map has GENERIC_HOME row", any(r["theme"] == "GENERIC_HOME" for r in tm))

print("\nASSERTIONS: %s" % ("ALL PASSED" if fails == 0 else "%d FAILED" % fails))
print("XLSX:", xlsx)
sys.exit(1 if fails else 0)
