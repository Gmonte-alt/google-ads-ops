#!/usr/bin/env python3
"""Synthetic validation for inventory_assessment.py. Builds a fixture DB exercising:
  - a PAUSED campaign whose ad groups must be excluded (re-enable constraint)
  - an account_id=2 row that must be excluded (multi-platform DB)
  - a 'Small-Carrier' grab-bag ad group DUPLICATED across two active campaigns (logical collapse)
  - per-campaign lead splits (leads_min/avg/max = the signal-density input)
No live DB touched; temp fixture only.
"""
import os
import sqlite3
import sys
import tempfile
import types

# stub sklearn (parity_common imports it transitively via usia_keyword_select)
for name in ["sklearn", "sklearn.feature_extraction", "sklearn.feature_extraction.text",
             "sklearn.metrics", "sklearn.metrics.pairwise"]:
    sys.modules[name] = types.ModuleType(name)
sys.modules["sklearn.feature_extraction.text"].TfidfVectorizer = object
sys.modules["sklearn.metrics.pairwise"].cosine_similarity = lambda *a, **k: None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inventory_assessment as inv  # noqa: E402

FX = os.path.join(tempfile.gettempdir(), "inventory_fx.db")
OUT = os.path.join(tempfile.gettempdir(), "inventory_fx_out")


def build_fixture():
    if os.path.exists(FX):
        os.remove(FX)
    c = sqlite3.connect(FX)
    c.executescript("""
        CREATE TABLE campaigns (account_id INT, campaign_id TEXT, campaign_name TEXT, status TEXT);
        CREATE TABLE ad_groups (account_id INT, ad_group_id TEXT, campaign_id TEXT, ad_group_name TEXT, status TEXT);
        CREATE TABLE keyword_instances (account_id INT, ad_group_id TEXT, campaign_id TEXT,
                                        keyword_text TEXT, match_type TEXT, status TEXT);
        CREATE TABLE keyword_performance_daily (account_id INT, ad_group_id TEXT, device TEXT,
                                        report_date TEXT, primary_conv_count REAL, primary_conv_value REAL);
        CREATE TABLE brand_registry (id INT, brand_name TEXT, normalized_name TEXT, bid_status TEXT,
                                     brand_category TEXT, verticals TEXT);
        CREATE TABLE brand_registry_keywords (brand_id INT, normalized TEXT);
    """)
    # brands (canonical len>=4 embed-match; Kin needs exact variants)
    c.executemany("INSERT INTO brand_registry VALUES (?,?,?,?,?,?)", [
        (1, "Geico", "geico", "approved", "carrier", "home"),
        (2, "Hartford", "hartford", "approved", "carrier", "home"),
        (3, "Bamboo", "bamboo", "approved", "carrier", "home"),
        (4, "Kin", "kin", "conditional", "carrier", "home"),
    ])
    c.executemany("INSERT INTO brand_registry_keywords VALUES (?,?)",
                  [(4, "kin insurance"), (4, "kin home insurance")])
    # campaigns: 2 active (acct1), 1 paused (acct1), 1 active but acct2
    c.executemany("INSERT INTO campaigns VALUES (?,?,?,?)", [
        (1, "C1", "TIER ONE", "ENABLED"),
        (1, "C2", "CALIFORNIA", "ENABLED"),
        (1, "C3", "OLD TIER THREE", "PAUSED"),
        (2, "C9", "MS-HOME", "ENABLED"),
    ])
    # ad groups
    c.executemany("INSERT INTO ad_groups VALUES (?,?,?,?,?)", [
        (1, "11", "C1", "Small-Carrier", "ENABLED"),
        (1, "12", "C1", "Geico", "ENABLED"),
        (1, "21", "C2", "Small-Carrier", "ENABLED"),   # duplicate name across campaign
        (1, "22", "C2", "Geico", "ENABLED"),
        (1, "31", "C3", "Kin", "ENABLED"),             # in PAUSED campaign -> must be excluded
        (2, "91", "C9", "Small-Carrier", "ENABLED"),   # acct2 -> excluded
    ])
    # keyword instances
    rows = []
    for agid, cid in [("11", "C1"), ("21", "C2")]:  # Small-Carrier grab-bag (both active campaigns)
        rows += [
            (1, agid, cid, "kin insurance", "EXACT", "ENABLED"),
            (1, agid, cid, "kin home insurance", "BROAD", "ENABLED"),
            (1, agid, cid, "bamboo insurance", "EXACT", "ENABLED"),
            (1, agid, cid, "hartford insurance", "PHRASE", "PAUSED"),
        ]
    for agid, cid in [("12", "C1"), ("22", "C2")]:  # Geico dedicated
        rows += [
            (1, agid, cid, "geico", "BROAD", "ENABLED"),
            (1, agid, cid, "geico home insurance", "EXACT", "ENABLED"),
        ]
    rows.append((1, "31", "C3", "kin insurance", "EXACT", "ENABLED"))  # paused campaign
    rows.append((2, "91", "C9", "geico", "BROAD", "ENABLED"))          # acct2
    c.executemany("INSERT INTO keyword_instances VALUES (?,?,?,?,?,?)", rows)
    # leads (device='ALL', recent). Split differs per campaign to test min/max.
    c.executemany("INSERT INTO keyword_performance_daily VALUES (?,?,?,?,?,?)", [
        (1, "11", "ALL", "date('now')", 40.0, 2000.0),  # placeholder; fix below with real date
    ])
    c.execute("DELETE FROM keyword_performance_daily")
    c.executemany(
        "INSERT INTO keyword_performance_daily (account_id, ad_group_id, device, report_date, "
        "primary_conv_count, primary_conv_value) VALUES (?,?,?,date('now','-5 day'),?,?)",
        [(1, "11", "ALL", 40.0, 2000.0), (1, "21", "ALL", 15.0, 800.0),
         (1, "12", "ALL", 80.0, 5000.0), (1, "22", "ALL", 30.0, 1900.0),
         (1, "11", "SEARCH", 999.0, 0.0)])  # non-ALL device must be ignored
    c.commit()
    c.close()


def main():
    build_fixture()
    inv.run(FX, days=90, out_dir=OUT, brand_db=FX)
    import csv
    logical = {r["ad_group"]: r for r in csv.DictReader(open(os.path.join(OUT, "adgroup_logical.csv")))}
    phys = list(csv.DictReader(open(os.path.join(OUT, "adgroup_physical.csv"))))
    carriers = {r["carrier"]: r for r in csv.DictReader(open(os.path.join(OUT, "carrier_coverage.csv")))}
    camps = {r["campaign"]: r for r in csv.DictReader(open(os.path.join(OUT, "campaign_summary.csv")))}

    fails = []

    def ck(cond, msg):
        if not cond:
            fails.append(msg)

    # paused campaign + acct2 excluded
    ck("Kin" not in logical, "paused-campaign ad group 'Kin' should be excluded")
    ck(all(p["campaign"] != "MS-HOME" for p in phys), "acct2 campaign should be excluded")
    ck("OLD TIER THREE" not in camps, "paused campaign should not appear in summary")

    # logical collapse of the duplicated grab-bag
    sc = logical.get("Small-Carrier")
    ck(sc is not None, "Small-Carrier logical row missing")
    if sc:
        ck(int(sc["n_campaigns"]) == 2, "Small-Carrier should span 2 campaigns, got %s" % sc["n_campaigns"])
        ck(abs(float(sc["total_leads"]) - 55.0) < 0.01, "Small-Carrier total_leads should be 55, got %s" % sc["total_leads"])
        ck(abs(float(sc["leads_max"]) - 40.0) < 0.01, "leads_max should be 40, got %s" % sc["leads_max"])
        ck(abs(float(sc["leads_min"]) - 15.0) < 0.01, "leads_min should be 15, got %s" % sc["leads_min"])
        ck(int(sc["distinct_carriers"]) == 3, "Small-Carrier should cover 3 carriers (Kin/Bamboo/Hartford), got %s" % sc["distinct_carriers"])
        ck(int(sc["core"]) >= 6 and int(sc["generic_home"]) >= 2,
           "Small-Carrier theme mix off: core=%s gen=%s" % (sc["core"], sc["generic_home"]))
        ck(int(sc["exact"]) >= 4 and int(sc["broad"]) >= 2 and int(sc["phrase"]) >= 2,
           "Small-Carrier match mix off: e=%s b=%s p=%s" % (sc["exact"], sc["broad"], sc["phrase"]))

    # geico dedicated, leads summed, non-ALL device ignored
    g = logical.get("Geico")
    ck(g is not None and abs(float(g["total_leads"]) - 110.0) < 0.01,
       "Geico total_leads should be 110 (non-ALL device ignored), got %s" % (g and g["total_leads"]))

    # carrier coverage bridge
    ck("Geico" in carriers and "Geico" in carriers["Geico"]["ad_groups"], "Geico coverage wrong")
    ck("Kin" in carriers and "Small-Carrier" in carriers["Kin"]["ad_groups"], "Kin should map to Small-Carrier")
    if "Kin" in carriers:
        ck(int(carriers["Kin"]["bare_core"]) >= 2, "Kin should show bare/CORE variants, got %s" % carriers["Kin"]["bare_core"])
        ck(int(carriers["Kin"]["home_generic"]) >= 2, "Kin should show home/GEN variants, got %s" % carriers["Kin"]["home_generic"])
    if "Geico" in carriers:
        ck(int(carriers["Geico"]["bare_core"]) >= 2 and int(carriers["Geico"]["home_generic"]) >= 2,
           "Geico should show both bare and home variants")

    print()
    if fails:
        print("ASSERTIONS FAILED:")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("ASSERTIONS: ALL PASSED")
    print("fixture out:", OUT)


if __name__ == "__main__":
    main()
