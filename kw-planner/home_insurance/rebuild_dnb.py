#!/usr/bin/env python
"""Rebuild NEG_BRAND_DNB from the registry's restricted-brand set (not 'brands we didn't
conquest'). Restricted brands -> phrase-token negatives (collision-guarded against positives).
Existing DNB queries covered by a restricted token are dropped as redundant; queries covered by
NO restricted token are RELEASED to eligible and reported for review (these are the biddable
brands like Erie). Lead-buyer aggregators we can't bid on are added explicitly."""
import csv, re, openpyxl

OUT = "/mnt/user-data/outputs/"
UP  = "/mnt/user-data/uploads/"
GEN, CONQ, BGEN = UP+"usia_home_keywords_v2.xlsx", UP+"usia_home_brand_v2.xlsx", UP+"usia_home_brand_generic.xlsx"

# brands whose registry name is a single generic English word -> require " insurance" to be safe
GENERIC_SUFFIX = {"peak","alpha","allied","cypress","protective"}
# lead-buyer aggregators (our customers/competitors): not in carrier registry, never biddable
LEAD_AGGREGATORS = ["everquote","smartfinancial","quotewizard","insurify","matic","otto","policygenius"]

def kws(path, sheet, col):
    w = openpyxl.load_workbook(path, data_only=True)[sheet]
    h = next(i for i,r in enumerate(w.iter_rows(values_only=True),1) if r and str(r[0]).strip()=="Campaign")
    return [str(r[col]).strip().lower() for r in w.iter_rows(min_row=h+1, values_only=True) if r and len(r)>col and r[col]]

pos_all = set(kws(GEN,"Selected Keywords",2) + kws(CONQ,"Brand Keywords",3) + kws(BGEN,"Keywords",2))

def rx(t): return re.compile(r"\b"+re.escape(t)+r"\b")

def tokenize(brand):
    t = brand.strip().lower()
    if t.startswith("citizens property"): return "citizens property"  # FL carrier; bare 'citizens' would block 'senior citizens ...'
    if t in GENERIC_SUFFIX: return t+" insurance"
    return t

# variant forms Google treats as distinct phrases (spaced/singular/short-form) + canonical-restricted leaks
EXTRA_VARIANTS = {
    "allstate": ["all state"],
    "liberty mutual": ["liberty"],
    "garrison property and casualty": ["garrison"],
    "united p&c": ["united property and casualty"],
    "united fire and casualty": ["united fire"],
    "selective insurance": ["selective"],
    "wright flood": ["wright", "wrights"],
}
ADD_RESTRICTED = ["brighthouse", "bright house"]   # restricted in canonical registry; leaked as a home query

# 1) restricted-brand tokens
restricted = [l.strip() for l in open(OUT+"restricted_home_brands.txt") if l.strip()]
tokens = {tokenize(b) for b in restricted} | set(LEAD_AGGREGATORS) | set(ADD_RESTRICTED)
for base, alts in EXTRA_VARIANTS.items():
    tokens |= set(alts)
# minimal-root reduction: drop a token if a shorter token already phrase-matches inside it
tokens = sorted(tokens, key=len)
minimal = []
for t in tokens:
    if not any(rx(s).search(t) for s in minimal): minimal.append(t)
tokens = sorted(minimal)

# 2) collision guard: a token that appears inside a positive keyword would block a brand we bid on
safe_tokens, clipped = [], []
for t in tokens:
    (clipped if any(rx(t).search(p) for p in pos_all) else safe_tokens).append(t)

# 3) reclassify existing DNB queries
old = []
with open(OUT+"NEG_BRAND_DNB.csv.bak", encoding="utf-8") as f:
    r = csv.reader(f); next(r)
    old = [row[0] for row in r if row]
covered, released = [], []
for q in old:
    if any(rx(t).search(q) for t in safe_tokens): covered.append(q)      # redundant w/ token -> drop
    else: released.append(q)                                            # biddable -> eligible

# 4) final list = safe restricted tokens (+ keep any covered query? no, redundant). Final collision sweep.
final = sorted(set(safe_tokens))
final = [t for t in final if not any(rx(t).search(p) for p in pos_all)]

with open(OUT+"NEG_BRAND_DNB.csv","w",newline="",encoding="utf-8") as f:
    w = csv.writer(f); w.writerow(["keyword","match_type","ui_paste_format"])
    for t in final: w.writerow([t,"phrase",'"%s"'%t])

print("restricted brands (home): %d  ->  tokens: %d" % (len(restricted), len(set(tokenize(b) for b in restricted))))
print("collision-clipped tokens (overlap a brand we bid on): %s" % (", ".join(clipped) or "none"))
print("NEG_BRAND_DNB: %d (old) -> %d (new tokens)" % (len(old), len(final)))
print("  redundant queries dropped (covered by a token): %d" % len(covered))
print("  queries RELEASED to eligible (no restricted token -> biddable): %d" % len(released))
print("\nfinal DNB tokens:\n  " + ", ".join(final))
print("\n--- RELEASED queries (now eligible - eyeball for any restricted brand my tokens missed) ---")
for q in sorted(released): print("  "+q)
