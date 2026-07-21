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
        CREATE TABLE keyword_instances (account_id INT, criterion_id INT, id INTEGER, ad_group_id TEXT,
                                        campaign_id TEXT, keyword_text TEXT, match_type TEXT, status TEXT);
        CREATE TABLE keyword_performance_daily (account_id INT, ad_group_id TEXT, device TEXT,
                                        report_date TEXT, primary_conv_count REAL, primary_conv_value REAL);
        CREATE TABLE brand_registry (id INT, brand_name TEXT, normalized_name TEXT, bid_status TEXT,
                                     brand_category TEXT, verticals TEXT);
        CREATE TABLE brand_registry_keywords (brand_id INT, normalized TEXT);
        CREATE VIEW keyword_current AS
          SELECT ki.account_id,
                 (ki.ad_group_id || '~' || ki.criterion_id) AS ad_group_criterion_id,
                 ki.criterion_id, ki.keyword_text, ki.match_type,
                 ki.status AS keyword_status,
                 ki.campaign_id, c.campaign_name, c.status AS campaign_status,
                 ki.ad_group_id, ag.ad_group_name, ag.status AS ad_group_status,
                 (CASE WHEN c.status='ENABLED' AND ag.status='ENABLED' THEN 1 ELSE 0 END) AS hierarchy_enabled,
                 (CASE WHEN c.status='ENABLED' AND ag.status='ENABLED' AND ki.status='ENABLED'
                       THEN 1 ELSE 0 END) AS is_serving
          FROM keyword_instances ki
          JOIN ad_groups ag ON ag.ad_group_id=ki.ad_group_id AND ag.account_id=ki.account_id
          JOIN campaigns c ON c.campaign_id=ki.campaign_id AND c.account_id=ki.account_id;
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
    # keyword instances -- modeling the REAL shape the diagnostic revealed: criterion_id is SHARED across
    # ad groups (it's the keyword criterion, not the instance), and there is NO row-level duplication --
    # every (ad_group_id, criterion_id) is unique = one instance. So counts must NOT GROUP BY criterion_id.
    # cols: account_id, criterion_id, id, ad_group_id, campaign_id, keyword_text, match_type, status
    rows = [
        # kin insurance: SAME criterion_id 9001 in both campaigns' Small-Carrier (different ad_group_ids)
        (1, 9001, 1,  "11", "C1", "kin insurance", "EXACT", "ENABLED"),
        (1, 9001, 5,  "21", "C2", "kin insurance", "EXACT", "ENABLED"),
        (1, 9002, 2,  "11", "C1", "kin home insurance", "BROAD", "ENABLED"),
        (1, 9002, 6,  "21", "C2", "kin home insurance", "BROAD", "ENABLED"),
        (1, 9003, 3,  "11", "C1", "bamboo insurance", "EXACT", "ENABLED"),
        (1, 9003, 7,  "21", "C2", "bamboo insurance", "EXACT", "ENABLED"),
        (1, 9004, 4,  "11", "C1", "hartford insurance", "PHRASE", "ENABLED"),
        (1, 9004, 8,  "21", "C2", "hartford insurance", "PHRASE", "ENABLED"),
        # Geico dedicated group (auto-detect pause must SKIP it)
        (1, 9005, 9,  "12", "C1", "geico", "BROAD", "ENABLED"),
        (1, 9006, 10, "12", "C1", "geico home insurance", "EXACT", "ENABLED"),
        (1, 9005, 11, "22", "C2", "geico", "BROAD", "ENABLED"),
        (1, 9006, 12, "22", "C2", "geico home insurance", "EXACT", "ENABLED"),
        # kin in PAUSED campaign C3 -> excluded via active-campaign filter
        (1, 9001, 13, "31", "C3", "kin insurance", "EXACT", "ENABLED"),
        # account 2 -> excluded
        (2, 9005, 14, "91", "C9", "geico", "BROAD", "ENABLED"),
    ]
    c.executemany("INSERT INTO keyword_instances VALUES (?,?,?,?,?,?,?,?)", rows)
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
    inv.run(FX, days=90, out_dir=OUT, brand_db=FX,
            pause_carriers=["Kin", "Geico"], pause_grabbag=None)  # auto-detect
    import csv
    logical = {r["ad_group"]: r for r in csv.DictReader(open(os.path.join(OUT, "adgroup_logical.csv")))}
    phys = list(csv.DictReader(open(os.path.join(OUT, "adgroup_physical.csv"))))
    carriers = {r["carrier"]: r for r in csv.DictReader(open(os.path.join(OUT, "carrier_coverage.csv")))}
    camps = {r["campaign"]: r for r in csv.DictReader(open(os.path.join(OUT, "campaign_summary.csv")))}
    pause = list(csv.DictReader(open(os.path.join(OUT, "pause_split.csv"))))

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

    # pause-split reads corrected keyword_current, emits Editor-matchable split IDs, pauses only serving:
    ck(len(pause) == 4, "pause should emit 4 Kin rows (2 kw x 2 campaigns), got %d" % len(pause))
    ck(all(p["Status"] == "Paused" for p in pause), "pause rows must be Status=Paused")
    ck(all(p.get("Keyword ID") and p.get("Ad group ID") for p in pause),
       "every pause row must carry separate Ad group ID + Keyword ID (Editor matches on the pair)")
    # Keyword ID is the numeric criterion (shared), Ad group ID disambiguates the placement
    ck({p["Keyword ID"] for p in pause} == {"9001", "9002"},
       "Keyword IDs should be the criterion numbers {9001,9002}, got %s" % {p["Keyword ID"] for p in pause})
    ck({p["Ad group ID"] for p in pause} == {"11", "21"},
       "Ad group IDs should be {11,21}, got %s" % {p["Ad group ID"] for p in pause})
    ck(all("kin" in p["Keyword"].lower() for p in pause), "pause set must be Kin-only (Geico skipped)")
    ck(not any("geico" in p["Keyword"].lower() for p in pause), "Geico must be skipped (own group)")
    kin_ins = [p for p in pause if p["Keyword"] == "kin insurance"]
    ck(len(kin_ins) == 2 and all(p["Match Type"] == "Exact" for p in kin_ins),
       "'kin insurance' should be Exact x2 (corrected match_type from keyword_current) -> %s"
       % [p["Match Type"] for p in kin_ins])
    # C3 kin (paused campaign) excluded by is_serving/campaign filter; acct2 excluded
    ck(len(pause) == 4, "C3 (paused campaign) kin must not appear -- is_serving/hierarchy gate")

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
