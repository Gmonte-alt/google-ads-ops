#!/usr/bin/env python
"""
brand_matcher.py -- reusable keyword->brand matcher, extracted verbatim from the logic in
kw_db_scripts/09_tag_keyword_identities.py so that ANY consumer (the change-history carrier
enricher, ad-hoc analyses, etc.) classifies a keyword to a brand the SAME way the canonical
tagger does. If two systems match carriers differently, their counts won't reconcile -- this
module exists so they can't diverge.

WHAT IT DOES
  Given a keyword/query string, finds the brand it references using the same three-layer pipeline
  as 09:
    Layer 1  exact full-string match against brand_registry_keywords variants
    Layer 2  substring scan using pre-compiled WORD-BOUNDARY patterns, sorted LONGEST-FIRST
             (so 'travelers auto and home insurance' matches contained 'travelers', and the most
             specific variant wins -- 'geico home insurance' over bare 'geico')
    Layer 3  fuzzy (Levenshtein) match for misspellings (geico->gecko), gated by FUZZY_MIN_LEN

  The word-boundary regex (?<![a-z])variant(?![a-z]) is what prevents 'kin' matching inside
  'skiing' while still matching short unambiguous brands (geico/aig/aaa).

CARRIER RULE (applied on top, per the settled definition):
  A matched brand is a HOME-RELEVANT CARRIER iff
     brand_category LIKE 'carrier_%'  AND  'home' in verticals.
  Use is_home_carrier() for that decision. This mirrors the SQL filter
  `brand_category LIKE 'carrier_%' AND verticals LIKE '%home%'`.

USAGE (change-history enricher):
    from brand_matcher import BrandMatcher
    bm = BrandMatcher(conn)                 # loads registry once
    m = bm.match("travelers auto and home insurance")
    # m -> BrandMatch(brand_name='Travelers', bid_status='approved',
    #                 verticals='auto,home', brand_category='carrier_approved',
    #                 method='substring', is_home_carrier=True) or None
"""
import re
from collections import namedtuple

# --- tunables (mirror 09_tag_keyword_identities.py) ---
SUBSTR_MIN_LEN  = 3    # shortest variant we'll containment-match (word-boundary gated)
FUZZY_MIN_LEN   = 6    # only fuzzy-match tokens/brands at least this long
FUZZY_THRESHOLD = 1    # max Levenshtein distance for a fuzzy hit

# Tokens that are too generic/ambiguous to fuzzy-match on their own.
FUZZY_EXCLUDE_TOKENS = {
    "insurance", "quote", "quotes", "home", "auto", "cheap", "online",
    "company", "near", "best", "policy", "coverage",
}

BrandMatch = namedtuple(
    "BrandMatch",
    ["brand_name", "bid_status", "verticals", "brand_category", "method", "is_home_carrier"],
)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().strip())


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        cur = [i + 1]
        for j, cb in enumerate(b):
            cur.append(min(prev[j + 1] + 1, cur[j] + 1, prev[j] + (ca != cb)))
        prev = cur
    return prev[-1]


def is_home_carrier(brand_category: str, verticals: str) -> bool:
    """Settled carrier rule: carrier_% category AND offers home."""
    cat = (brand_category or "")
    verts = (verticals or "")
    return cat.startswith("carrier_") and ("home" in [v.strip() for v in verts.split(",")])


class BrandMatcher:
    """Loads the brand registry once, then matches keyword strings to brands.

    Mirrors load_brand_registry() + classify_keyword()'s brand layers from
    09_tag_keyword_identities.py. Pass an open sqlite3 connection (row_factory not required).
    """

    def __init__(self, conn):
        cur = conn.cursor()

        # exact variant map: normalized_variant -> (brand, bid_status, verticals, category)
        rows = cur.execute("""
            SELECT brk.normalized, br.brand_name, br.bid_status,
                   br.verticals, br.brand_category
            FROM brand_registry_keywords brk
            JOIN brand_registry br ON brk.brand_id = br.id
            WHERE br.normalized_name != 'unattributed_negative_keyword'
        """).fetchall()

        self.exact_map = {}
        for r in rows:
            norm, brand, status, verts, cat = r[0], r[1], r[2], r[3] or "", r[4] or ""
            self.exact_map[norm] = (brand, status, verts, cat)

        # pre-compiled word-boundary patterns, sorted LONGEST-FIRST (most specific wins)
        self.compiled = []
        for variant, (brand, status, verts, cat) in self.exact_map.items():
            if not variant or len(variant) < SUBSTR_MIN_LEN:
                continue
            try:
                pat = re.compile(r"(?<![a-z])" + re.escape(variant) + r"(?![a-z])")
                self.compiled.append((pat, brand, status, verts, cat, len(variant)))
            except re.error:
                pass
        self.compiled.sort(key=lambda x: x[5], reverse=True)

        # fuzzy candidates: canonical brand normalized names, longest-first
        frows = cur.execute("""
            SELECT normalized_name, brand_name, bid_status, verticals, brand_category
            FROM brand_registry
            WHERE normalized_name != 'unattributed_negative_keyword'
            ORDER BY LENGTH(normalized_name) DESC
        """).fetchall()
        self.fuzzy = [
            (r[0], r[1], r[2], r[3] or "", r[4] or "") for r in frows
        ]

    def match(self, kw_text: str):
        """Return a BrandMatch, or None if no brand is found."""
        q = normalize(kw_text).replace("+", " ")
        q_clean = re.sub(r"\s+", " ", q).strip()
        if not q_clean:
            return None
        tokens = q_clean.split()

        # Layer 1: exact full-string
        if q_clean in self.exact_map:
            brand, status, verts, cat = self.exact_map[q_clean]
            return BrandMatch(brand, status, verts, cat, "exact",
                              is_home_carrier(cat, verts))

        # Layer 2: substring scan, longest-first (first hit = most specific)
        for pat, brand, status, verts, cat, _vlen in self.compiled:
            if pat.search(q_clean):
                return BrandMatch(brand, status, verts, cat, "substring",
                                  is_home_carrier(cat, verts))

        # Layer 3: fuzzy (misspellings)
        fz = self._fuzzy(tokens)
        if fz:
            brand, status, verts, cat = fz
            return BrandMatch(brand, status, verts, cat, "fuzzy",
                              is_home_carrier(cat, verts))

        return None

    def _fuzzy(self, tokens):
        best = None
        best_dist = FUZZY_THRESHOLD + 1
        for norm_brand, brand, status, verts, cat in self.fuzzy:
            if len(norm_brand) < FUZZY_MIN_LEN:
                continue
            bt = norm_brand.split()
            if len(bt) == 1:
                for tok in tokens:
                    if len(tok) < FUZZY_MIN_LEN or tok in FUZZY_EXCLUDE_TOKENS:
                        continue
                    d = _levenshtein(tok, norm_brand)
                    if d < best_dist:
                        best_dist, best = d, (brand, status, verts, cat)
            elif len(bt) == 2:
                for i in range(len(tokens) - 1):
                    pair = tokens[i] + " " + tokens[i + 1]
                    d = _levenshtein(pair, norm_brand)
                    if d <= FUZZY_THRESHOLD and d < best_dist:
                        best_dist, best = d, (brand, status, verts, cat)
        return best if (best and best_dist <= FUZZY_THRESHOLD) else None


# --- quick self-test / demo ---
if __name__ == "__main__":
    import sqlite3, sys
    dbp = sys.argv[1] if len(sys.argv) > 1 else r"C:\Projects\google-ads-ops\db\paid_search.db"
    conn = sqlite3.connect("file:%s?mode=ro" % dbp, uri=True)
    bm = BrandMatcher(conn)
    print("loaded: %d exact variants, %d compiled patterns, %d fuzzy candidates\n"
          % (len(bm.exact_map), len(bm.compiled), len(bm.fuzzy)))
    tests = [
        "travelers auto and home insurance",   # substring -> Travelers, home carrier
        "geico home insurance",                # substring -> Geico
        "aetna casualty",                      # -> Aetna, NOT home carrier (medicare,life)
        "state farm",                          # exact/substring -> State Farm
        "dollar general",                      # ambiguous -> ideally no carrier
        "costco condo insurance",              # generic/partner, not a carrier brand
    ]
    for t in tests:
        m = bm.match(t)
        if m:
            print("  %-38s -> %-18s cat=%-16s home_carrier=%s (%s)"
                  % (t, m.brand_name, m.brand_category, m.is_home_carrier, m.method))
        else:
            print("  %-38s -> (no brand)" % t)
