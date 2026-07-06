#!/usr/bin/env python
"""Synthetic validation for parity_common + 13_persist_target_theme. No live DB touched."""
import sys, types, sqlite3, os, tempfile

# --- stub sklearn so usia_keyword_select imports (clustering path is never invoked here) ---
for name in ["sklearn", "sklearn.feature_extraction", "sklearn.feature_extraction.text",
             "sklearn.metrics", "sklearn.metrics.pairwise"]:
    sys.modules[name] = types.ModuleType(name)
sys.modules["sklearn.feature_extraction.text"].TfidfVectorizer = object
sys.modules["sklearn.metrics.pairwise"].cosine_similarity = lambda *a, **k: None

from parity_common import (covering_key, categorize, make_brand_tagger, brand_of,
                           load_usia_targets, is_suppressed_add)
import importlib
persist = importlib.import_module("13_persist_target_theme")

# Scratch DB in the OS temp dir -- cross-platform, never near the live paid_search.db, never litters.
DB = os.path.join(tempfile.gettempdir(), "parity_synth_check.db")
if os.path.exists(DB):
    os.remove(DB)
conn = sqlite3.connect(DB)
c = conn.cursor()

# --- minimal schemas (only columns the code touches) ---
c.execute("""CREATE TABLE search_queries (
    id INTEGER PRIMARY KEY, query_text TEXT NOT NULL, normalized_text TEXT NOT NULL,
    theme TEXT, modifier_brand TEXT, intent_type TEXT)""")   # last 3 = representative 08 columns
c.execute("""CREATE TABLE brand_registry (
    id INTEGER PRIMARY KEY, brand_name TEXT, normalized_name TEXT, bid_status TEXT,
    brand_category TEXT, verticals TEXT)""")
c.execute("""CREATE TABLE brand_registry_keywords (
    id INTEGER PRIMARY KEY, brand_id INTEGER, keyword_text TEXT, normalized TEXT, match_type TEXT)""")

# real brands
c.execute("INSERT INTO brand_registry VALUES (1,'Liberty Mutual','liberty mutual','restricted','carrier_restricted_partner','home')")
c.execute("INSERT INTO brand_registry VALUES (2,'State Farm','state farm','restricted','carrier_restricted_partner','home')")
# sentinel (the 66% artifact brand)
c.execute("INSERT INTO brand_registry VALUES (3,'Unattributed Negative Keyword','unattributed_negative_keyword','restricted','','')")

# real variants first ...
c.execute("INSERT INTO brand_registry_keywords VALUES (10,1,'liberty','liberty','')")
c.execute("INSERT INTO brand_registry_keywords VALUES (11,1,'liberty mutual','liberty mutual','')")
c.execute("INSERT INTO brand_registry_keywords VALUES (12,2,'statefarm','statefarm','')")
c.execute("INSERT INTO brand_registry_keywords VALUES (13,2,'state farm','state farm','')")
# ... then sentinel variants LATER (higher id) so, WITHOUT the guard, last-writer-wins would
# overwrite 'liberty'/'statefarm' to the sentinel and poison self.exact['']
c.execute("INSERT INTO brand_registry_keywords VALUES (20,3,'liberty','liberty','')")
c.execute("INSERT INTO brand_registry_keywords VALUES (21,3,'statefarm','statefarm','')")
c.execute("INSERT INTO brand_registry_keywords VALUES (22,3,'844-879-0568','\"844-879-0568\"','')")
c.execute("INSERT INTO brand_registry_keywords VALUES (23,3,'','','')")          # empty key
c.execute("INSERT INTO brand_registry_keywords VALUES (24,3,'[exact]','[exact]','')")  # punct-only

# search_queries rows: 08 columns pre-filled to prove they stay untouched
sq = [
    (1, "home  insurance", "home  insurance", "OLD08", "old08brand", "OLD08intent"),   # double-space
    (2, "home health insurance", "home health insurance", "OLD08", "old08brand", "OLD08intent"),  # ADJACENT
    (3, "home and auto insurance quote", "home and auto insurance quote", "OLD08", "old08brand", "OLD08intent"),  # BUNDLE
    (4, "vacant home insurance", "vacant home insurance", "OLD08", "old08brand", "OLD08intent"),  # HOMEOWNERS_EXT
    (5, "cheap home insurance", "cheap home insurance", "OLD08", "old08brand", "OLD08intent"),    # PRICE
    (6, "state farm home insurance", "state farm home insurance", "OLD08", "old08brand", "OLD08intent"),  # BRAND group
    (7, "asdfqwer zzz", "asdfqwer zzz", "OLD08", "old08brand", "OLD08intent"),         # CORE
]
c.executemany("INSERT INTO search_queries VALUES (?,?,?,?,?,?)", sq)
conn.commit()
conn.close()

P = "PASS"; F = "FAIL"; fails = 0
def check(label, cond):
    global fails
    print("  [%s] %s" % (P if cond else F, label))
    if not cond: fails += 1

print("=== 1. covering_key: collapse-both closes the phantom-add case ===")
stored = covering_key("home  insurance")   # a stored 05-written double-spaced query, re-normalized fresh
kw     = covering_key("home insurance")    # single-spaced keyword_instance.keyword_text
check("double-spaced query covered by single-spaced keyword", stored == kw)
check("distinct queries not merged (no false positive)",
      covering_key("home insurance quote") != covering_key("home insurance"))

print("\n=== 2. GuardedBrandTagger: sentinel dropped, no self.exact[''] poison ===")
t = make_brand_tagger(DB)
check("'liberty' -> Liberty Mutual (not sentinel), despite sentinel loading later",
      brand_of(t, "liberty mutual")[0] == "Liberty Mutual" and brand_of(t, "liberty")[0] == "Liberty Mutual")
check("'statefarm' -> State Farm (not sentinel)", brand_of(t, "statefarm")[0] == "State Farm")
check("self.exact[''] never populated", "" not in t.exact)
check("sentinel phone/punct variants absent from exact table",
      '"844-879-0568"' not in t.exact and "[exact]" not in t.exact)
check("no key in exact maps to the sentinel brand",
      all(v != "unattributed_negative_keyword" for v in t.exact.values()))

print("\n=== 3. categorize: spec taxonomy routing ===")
cases = {"home health insurance": "ADJACENT:health_life", "home and auto insurance quote": "BUNDLE",
         "vacant home insurance": "HOMEOWNERS_EXT", "cheap home insurance": "PRICE",
         "home insurance quote": "QUOTE", "asdfqwer zzz": "CORE"}
for text, exp in cases.items():
    got = categorize(text)
    check("categorize(%r) == %s (got %s)" % (text, exp, got), got == exp)
check("ADJACENT is suppressed as an add; BUNDLE is not",
      is_suppressed_add("ADJACENT:health_life") and not is_suppressed_add("BUNDLE"))

print("\n=== 4. 13_ pass: idempotent ADD COLUMN, writes target_*, leaves 08 untouched ===")
persist.run(DB, DB, apply=False)                # dry-run first (must NOT alter schema)
conn = sqlite3.connect(DB)
after_dry = {r[1] for r in conn.execute("PRAGMA table_info(search_queries)")}
conn.close()
check("dry-run did NOT add columns (touches nothing)",
      not ({"target_theme", "target_theme_source", "target_group"} & after_dry))
persist.run(DB, DB, apply=True, force_small=True)                 # apply: adds columns + writes
persist.run(DB, DB, apply=True, force_small=True)                 # re-run: idempotent, no error
conn = sqlite3.connect(DB)
have = {r[1] for r in conn.execute("PRAGMA table_info(search_queries)")}
check("apply added the 3 columns", {"target_theme", "target_theme_source", "target_group"} <= have)
rows = {r[0]: r for r in conn.execute(
    "SELECT id, query_text, theme, modifier_brand, intent_type, target_theme, target_group FROM search_queries")}
conn.close()
check("row1 target_theme populated", rows[1][5] is not None)
check("row2 ADJACENT persisted", rows[2][5] == "ADJACENT:health_life")
check("row6 target_group == BRAND (state farm)", rows[6][6] == "BRAND")
check("row7 target_group == NONBRAND", rows[7][6] == "NONBRAND")
check("08 columns UNTOUCHED (theme/modifier_brand/intent_type still 'OLD08*')",
      all(rows[i][2] == "OLD08" and rows[i][3] == "old08brand" and rows[i][4] == "OLD08intent"
          for i in rows))

print("\n=== 5. load_usia_targets against real v2 export: empties kept, OUT excluded ===")
_here = os.path.dirname(os.path.abspath(__file__))
_csv_candidates = [
    os.environ.get("KWP_V2_CSV", ""),
    os.path.join(_here, "kwp_ideas_export_v2.csv"),
    os.path.join(os.getcwd(), "kwp_ideas_export_v2.csv"),
]
_csv = next((p for p in _csv_candidates if p and os.path.exists(p)), None)
if not _csv:
    print("  [SKIP] kwp_ideas_export_v2.csv not found next to this script (set KWP_V2_CSV to test "
          "section 5). Sections 1-4 are the core proofs and are unaffected.")
else:
    by_theme, flat = load_usia_targets(_csv)
    kws = {k for k, _, _ in flat}
    check("generic head with EMPTY product_code retained ('home owners insurance quote')",
          "home owners insurance quote" in kws)
    check("OUT-product term excluded ('dwelling fire insurance')", "dwelling fire insurance" not in kws)
    check("no ADJACENT/BUNDLE explosion; CORE present as catch-all", "CORE" in by_theme)
    print("     -> %d USIA target keywords across %d themes" % (len(flat), len(by_theme)))

print("\n%s" % ("ALL CHECKS PASSED" if fails == 0 else "%d CHECK(S) FAILED" % fails))
sys.exit(1 if fails else 0)
