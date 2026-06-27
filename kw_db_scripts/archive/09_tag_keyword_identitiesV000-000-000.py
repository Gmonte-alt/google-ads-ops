"""
09_tag_keyword_identities.py
------------------------------
Adds vertical, brand_category, modifier_brand, and tag_method columns
to keyword_identities and populates them using the same four-layer
classification pipeline used by 08_tag_search_queries.py.

Why keyword_identities and not keyword_instances?
    keyword_identities is the cross-platform dedup table -- one row per
    (normalized_text + match_type) regardless of campaign, ad group, or
    account. Tagging at this level means "Farmers Home Insurance BROAD"
    is tagged once and the tag applies to all 12 instances of that keyword
    across Google and Microsoft ad groups. This also eliminates the
    duplicate-row problem in the keyword efficiency analysis.

Classification uses the same brand_registry and signal lists as the
query tagger. Keyword text is typically cleaner than search queries
(no misspellings, no user intent noise) so exact match rates will be
higher and fuzzy match rates will be lower.

Usage:
    conda activate paid-search
    python kw_db_scripts/09_tag_keyword_identities.py

Safe to re-run -- uses UPDATE, not INSERT.
"""

import sqlite3
import re
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

# ---------------------------------------------------------------------------
# Signal lists (mirrors 08_tag_search_queries.py)
# ---------------------------------------------------------------------------

INSURANCE_SEED = {
    'insurance', 'insure', 'insured', 'insurer', 'insuring',
    'coverage', 'policy', 'policies', 'premium', 'deductible',
    'claim', 'homeowner', 'homeowners', 'renters', 'medicare',
}

VERTICAL_SIGNALS = {
    'home': [
        'home insurance', 'homeowners insurance', 'homeowner insurance',
        'homeowners', 'homeowner', 'house insurance', 'home owner',
        'dwelling insurance', 'property insurance', 'home coverage',
        'home policy', 'home quote', 'home insur',
    ],
    'renters': [
        'renters insurance', 'renter insurance', "renter's insurance",
        'renters coverage', 'apartment insurance', 'tenant insurance',
    ],
    'auto': [
        'auto insurance', 'car insurance', 'vehicle insurance',
        'auto coverage', 'auto policy', 'car policy', 'auto quote',
        'car quote', 'motorcycle insurance',
    ],
    'medicare': [
        'medicare', 'medicaid', 'medigap', 'medicare supplement',
        'medicare advantage', 'part a', 'part b', 'senior insurance',
    ],
    'life': [
        'life insurance', 'term life', 'whole life', 'universal life',
        'final expense', 'burial insurance',
    ],
}

BUNDLE_SIGNALS = {
    'home and auto', 'home & auto', 'home auto bundle',
    'bundle home', 'bundle auto', 'auto and home',
    'bundl', 'multi policy', 'multi-policy', 'package policy',
}

NEAR_ME_SIGNALS = {
    'near me', 'near by', 'nearby', 'close to me', 'in my area', 'local',
}

HOME_PURCHASE_SIGNALS = {
    'zillow', 'realtor', 'redfin', 'trulia', 'homes for sale',
    'home for sale', 'buying a home', 'buy a home', 'new home buyer',
    'first time home buyer', 'home loan', 'mortgage rate',
    'home purchase', 'escrow', 'closing costs', 'for sale by owner',
}

GEO_SIGNALS = {
    'alabama','alaska','arizona','arkansas','california','colorado',
    'connecticut','delaware','florida','georgia','hawaii','idaho',
    'illinois','indiana','iowa','kansas','kentucky','louisiana',
    'maine','maryland','massachusetts','michigan','minnesota',
    'mississippi','missouri','montana','nebraska','nevada',
    'new hampshire','new jersey','new mexico','new york',
    'north carolina','north dakota','ohio','oklahoma','oregon',
    'pennsylvania','rhode island','south carolina','south dakota',
    'tennessee','texas','utah','vermont','virginia','washington',
    'west virginia','wisconsin','wyoming',
    'calif','fla','tex','colo','conn','ariz','tenn','wash','minn',
}

PRICE_SIGNALS = {
    'cheap', 'cheapest', 'affordable', 'low cost', 'average cost',
    'how much', 'price', 'rates', 'rate', 'quotes', 'compare',
    'comparison', 'best rate', 'lowest', 'free quote', 'get quote',
    'online quote', 'instant quote', 'estimate', 'cost',
}

COVERAGE_SIGNALS = {
    'flood': 'flood', 'fire': 'fire', 'earthquake': 'earthquake',
    'wildfire': 'wildfire', 'landlord': 'landlord',
    'rental property': 'landlord', 'condo': 'condo',
    'condominium': 'condo', 'mobile home': 'mobile_home',
    'manufactured home': 'mobile_home', 'umbrella': 'umbrella',
    'wind': 'wind', 'hail': 'hail', 'hurricane': 'hurricane',
    'dwelling': 'dwelling', 'renters insurance': 'renters',
    'renter insurance': 'renters', 'renters': 'renters',
}

FUZZY_THRESHOLD = 2
FUZZY_MIN_LEN   = 6

# Tokens that must never be fuzzy-matched to a brand name.
# These are common insurance vocabulary words that happen to be within
# edit distance 2 of some brand names (e.g. 'insurance' vs 'esurance').
# Adding a token here forces exact-match-only for brand detection on that token.
FUZZY_EXCLUDE_TOKENS = {
    'insurance', 'insure', 'insured', 'insurer', 'coverage',
    'homeowners', 'homeowner', 'renters', 'renter', 'policy',
    'policies', 'premium', 'claim', 'quotes', 'quote',
    'auto', 'home', 'life', 'health', 'medicare', 'medicaid',
    'mortgage', 'general', 'mutual', 'national', 'american',
}
BATCH_SIZE      = 500

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn

# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def migrate_schema(conn):
    cursor = conn.cursor()
    existing = {r[1] for r in cursor.execute(
        "PRAGMA table_info(keyword_identities)"
    )}
    new_cols = [
        ("vertical",          "TEXT"),
        ("brand_category",    "TEXT"),
        ("modifier_brand",    "TEXT"),
        ("modifier_geo",      "TEXT"),
        ("modifier_price",    "INTEGER DEFAULT 0"),
        ("modifier_coverage", "TEXT"),
        ("modifier_bundle",   "INTEGER DEFAULT 0"),
        ("modifier_near_me",  "INTEGER DEFAULT 0"),
        ("modifier_home_purchase", "INTEGER DEFAULT 0"),
        ("tag_method",        "TEXT DEFAULT 'unclassified'"),
    ]
    for col, defn in new_cols:
        if col not in existing:
            cursor.execute(
                f"ALTER TABLE keyword_identities ADD COLUMN {col} {defn}"
            )
            print(f"  Added: keyword_identities.{col}")
        else:
            print(f"  Exists: keyword_identities.{col}")
    conn.commit()

# ---------------------------------------------------------------------------
# Levenshtein (same as query tagger)
# ---------------------------------------------------------------------------

def levenshtein(a: str, b: str) -> int:
    if a == b: return 0
    if len(a) < len(b): a, b = b, a
    if not b: return len(a)
    prev = list(range(len(b) + 1))
    for ca in a:
        curr = [prev[0] + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j+1]+1, curr[j]+1, prev[j]+(ca != cb)))
        prev = curr
    return prev[-1]

# ---------------------------------------------------------------------------
# Brand registry loader
# ---------------------------------------------------------------------------

def load_brand_registry(conn):
    cursor = conn.cursor()
    rows = cursor.execute("""
        SELECT brk.normalized, br.brand_name, br.bid_status,
               br.verticals, br.brand_category
        FROM brand_registry_keywords brk
        JOIN brand_registry br ON brk.brand_id = br.id
        WHERE br.normalized_name != 'unattributed_negative_keyword'
    """).fetchall()

    exact_map = {}
    for r in rows:
        exact_map[r['normalized']] = (
            r['brand_name'], r['bid_status'],
            r['verticals'] or '', r['brand_category'] or ''
        )

    fuzzy_list = cursor.execute("""
        SELECT normalized_name, brand_name, bid_status,
               verticals, brand_category
        FROM brand_registry
        WHERE normalized_name != 'unattributed_negative_keyword'
        ORDER BY LENGTH(normalized_name) DESC
    """).fetchall()

    fuzzy_candidates = [
        (r['normalized_name'], r['brand_name'],
         r['bid_status'], r['verticals'] or '',
         r['brand_category'] or '')
        for r in fuzzy_list
    ]
    print(f"  Brand registry: {len(exact_map):,} exact variants, "
          f"{len(fuzzy_candidates):,} fuzzy candidates")
    return exact_map, fuzzy_candidates

# ---------------------------------------------------------------------------
# Classification (same logic as 08_tag_search_queries.py)
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    return re.sub(r'\s+', ' ', text.lower().strip())


def fuzzy_brand_match(tokens, fuzzy_list):
    best_brand = best_status = best_verts = best_cat = None
    best_dist  = FUZZY_THRESHOLD + 1

    for norm_brand, brand_name, bid_status, verticals, brand_category in fuzzy_list:
        if len(norm_brand) < FUZZY_MIN_LEN:
            continue
        brand_tokens = norm_brand.split()

        if len(brand_tokens) == 1:
            for tok in tokens:
                if len(tok) < FUZZY_MIN_LEN:
                    continue
                if tok in FUZZY_EXCLUDE_TOKENS:
                    continue
                d = levenshtein(tok, norm_brand)
                if d < best_dist:
                    best_dist  = d
                    best_brand = brand_name
                    best_status = bid_status
                    best_verts = verticals
                    best_cat   = brand_category
        elif len(brand_tokens) == 2:
            for i in range(len(tokens) - 1):
                pair = tokens[i] + ' ' + tokens[i+1]
                d = levenshtein(pair, norm_brand)
                if d <= FUZZY_THRESHOLD and d < best_dist:
                    best_dist  = d
                    best_brand = brand_name
                    best_status = bid_status
                    best_verts = verticals
                    best_cat   = brand_category

    if best_brand and best_dist <= FUZZY_THRESHOLD:
        return best_brand, best_status, best_verts, best_cat
    return None


def infer_vertical_from_brand(brand_verticals: str, query: str) -> str:
    if not brand_verticals:
        return 'unclassified'
    verts = [v.strip() for v in brand_verticals.split(',')]
    if len(verts) == 1:
        return 'home' if verts[0] == 'renters' else verts[0]
    for vertical, signals in VERTICAL_SIGNALS.items():
        for sig in signals:
            if sig in query:
                return 'home' if vertical == 'renters' else vertical
    return 'cross'


def assign_vertical_from_signals(query: str):
    for vertical, signals in VERTICAL_SIGNALS.items():
        for sig in signals:
            if sig in query:
                return 'home' if vertical == 'renters' else vertical
    return None


def detect_modifiers(query: str) -> dict:
    result = {
        'modifier_geo':           None,
        'modifier_price':         0,
        'modifier_coverage':      None,
        'modifier_bundle':        0,
        'modifier_near_me':       0,
        'modifier_home_purchase': 0,
    }
    for geo in GEO_SIGNALS:
        if geo in query:
            result['modifier_geo'] = geo
            break
    for sig in PRICE_SIGNALS:
        if sig in query:
            result['modifier_price'] = 1
            break
    matched_cov, matched_len = None, 0
    for sig, cov_type in COVERAGE_SIGNALS.items():
        if sig in query and len(sig) > matched_len:
            matched_cov  = cov_type
            matched_len  = len(sig)
    result['modifier_coverage'] = matched_cov
    for sig in BUNDLE_SIGNALS:
        if sig in query:
            result['modifier_bundle'] = 1
            break
    for sig in NEAR_ME_SIGNALS:
        if sig in query:
            result['modifier_near_me'] = 1
            break
    for sig in HOME_PURCHASE_SIGNALS:
        if sig in query:
            result['modifier_home_purchase'] = 1
            break
    return result


def classify_keyword(kw_text: str, match_type: str,
                     exact_map: dict, fuzzy_list: list) -> dict:
    """
    Classify a keyword identity using the same pipeline as the query tagger.
    Keyword text is cleaner than search queries so exact match rates are higher.
    """
    q = normalize(kw_text)
    # Strip broad match modifier syntax (+keyword)
    q_clean = q.replace('+', ' ')
    q_clean = re.sub(r'\s+', ' ', q_clean).strip()
    tokens  = q_clean.split()

    result = {
        'vertical':          None,
        'brand_category':    None,
        'modifier_brand':    None,
        'modifier_geo':      None,
        'modifier_price':    0,
        'modifier_coverage': None,
        'modifier_bundle':   0,
        'modifier_near_me':  0,
        'modifier_home_purchase': 0,
        'tag_method':        'unclassified',
    }

    brand_name = bid_status = brand_verts = brand_cat = None
    brand_found = False

    # Layer 1: exact match against keyword variant table
    if q_clean in exact_map:
        brand_name, bid_status, brand_verts, brand_cat = exact_map[q_clean]
        brand_found = True
        result['tag_method'] = 'brand_exact'
    else:
        # Layer 1 substring: check if any known brand variant appears as a
        # whole-word substring in the keyword text.
        #
        # BUG FIX: previously used `len(variant) >= FUZZY_MIN_LEN` (≥6) here,
        # which silently skipped 5-char brands like 'geico' and 3-char brands
        # like 'kin' — causing multi-word keywords such as
        # 'geico homeowners insurance' and 'kin home insurance' to go untagged.
        # FUZZY_MIN_LEN is the right guard for *fuzzy* matching only; exact
        # substring matching only needs a minimum of 3 chars to be meaningful.
        #
        # Word-boundary check: use a regex so that a short variant like 'kin'
        # doesn't match inside unrelated words (e.g. 'skiing', 'working').
        # Longer variants (≥6 chars) are matched with a plain `in` check for
        # speed, since false-positive risk is negligible at that length.
        SUBSTR_MIN_LEN = 3
        best_variant_len = 0  # prefer longer variant matches (more specific)
        for variant, (b_name, b_status, b_verts, b_cat) in exact_map.items():
            if not variant or len(variant) < SUBSTR_MIN_LEN:
                continue
            # Check for whole-word occurrence
            if len(variant) >= FUZZY_MIN_LEN:
                matched = variant in q_clean
            else:
                matched = bool(re.search(
                    r'(?<![a-z])' + re.escape(variant) + r'(?![a-z])',
                    q_clean
                ))
            if matched and len(variant) > best_variant_len:
                brand_name  = b_name
                bid_status  = b_status
                brand_verts = b_verts
                brand_cat   = b_cat
                brand_found = True
                best_variant_len = len(variant)
                result['tag_method'] = 'brand_exact'
        # (loop completes; best/longest match wins)

    # Layer 2: fuzzy match
    if not brand_found:
        fuzzy_result = fuzzy_brand_match(tokens, fuzzy_list)
        if fuzzy_result:
            brand_name, bid_status, brand_verts, brand_cat = fuzzy_result
            brand_found = True
            result['tag_method'] = 'brand_fuzzy'

    # Relevance gate -- keywords are always insurance-relevant by definition
    # (they're in insurance ad accounts), so skip the seed term check

    if brand_found:
        result['modifier_brand'] = normalize(brand_name)
        result['brand_category'] = brand_cat or None
        result['vertical']       = infer_vertical_from_brand(brand_verts, q_clean)
    else:
        # Pattern match for non-brand keywords
        pattern_vertical = assign_vertical_from_signals(q_clean)
        if pattern_vertical:
            result['vertical']    = pattern_vertical
            result['tag_method']  = 'pattern'
        else:
            result['vertical']    = 'unclassified'
            result['tag_method']  = 'unclassified'

    # Modifiers apply to all keywords
    modifiers = detect_modifiers(q_clean)
    result.update(modifiers)

    # Coverage type can disambiguate vertical for non-brand keywords
    if result['vertical'] in (None, 'unclassified') and result['modifier_coverage']:
        result['vertical'] = 'home'
        if not brand_found:
            result['tag_method'] = 'pattern'

    return result

# ---------------------------------------------------------------------------
# Main tagging loop
# ---------------------------------------------------------------------------

def run_tagger(conn):
    cursor = conn.cursor()

    print("Loading brand registry...")
    exact_map, fuzzy_list = load_brand_registry(conn)

    # Fetch all keyword identities
    identities = cursor.execute(
        "SELECT id, normalized_text, match_type FROM keyword_identities"
    ).fetchall()
    total = len(identities)
    print(f"Tagging {total:,} keyword identities...")

    counts = {
        'brand_exact': 0, 'brand_fuzzy': 0,
        'pattern': 0, 'unclassified': 0,
    }
    vertical_counts = {}
    updates = []

    for i, row in enumerate(identities):
        kid       = row['id']
        kw_text   = row['normalized_text']
        match_type = row['match_type']

        result = classify_keyword(kw_text, match_type, exact_map, fuzzy_list)
        counts[result['tag_method']] = counts.get(result['tag_method'], 0) + 1
        vert = result['vertical'] or 'none'
        vertical_counts[vert] = vertical_counts.get(vert, 0) + 1

        updates.append((
            result['vertical'],
            result['brand_category'],
            result['modifier_brand'],
            result['modifier_geo'],
            result['modifier_price'],
            result['modifier_coverage'],
            result['modifier_bundle'],
            result.get('modifier_near_me', 0),
            result.get('modifier_home_purchase', 0),
            result['tag_method'],
            kid,
        ))

        if len(updates) >= BATCH_SIZE:
            _flush(cursor, updates)
            conn.commit()
            updates.clear()

        if (i + 1) % 10000 == 0:
            print(f"  Progress: {i+1:,}/{total:,}  "
                  f"exact={counts['brand_exact']:,}  "
                  f"fuzzy={counts.get('brand_fuzzy',0):,}  "
                  f"pattern={counts.get('pattern',0):,}  "
                  f"unclass={counts['unclassified']:,}")

    if updates:
        _flush(cursor, updates)
        conn.commit()

    return counts, vertical_counts


def _flush(cursor, updates):
    cursor.executemany("""
        UPDATE keyword_identities SET
            vertical               = ?,
            brand_category         = ?,
            modifier_brand         = ?,
            modifier_geo           = ?,
            modifier_price         = ?,
            modifier_coverage      = ?,
            modifier_bundle        = ?,
            modifier_near_me       = ?,
            modifier_home_purchase = ?,
            tag_method             = ?
        WHERE id = ?
    """, updates)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 65)
    print("09_tag_keyword_identities.py")
    print("=" * 65)

    conn = get_conn()

    print("\n[1] Migrating keyword_identities schema...")
    migrate_schema(conn)

    print("\n[2] Tagging keyword identities...")
    counts, vertical_counts = run_tagger(conn)

    total = sum(counts.values())

    # Summary
    cursor = conn.cursor()
    total_identities = cursor.execute(
        "SELECT COUNT(*) FROM keyword_identities"
    ).fetchone()[0]
    tagged = cursor.execute(
        "SELECT COUNT(*) FROM keyword_identities "
        "WHERE tag_method != 'unclassified'"
    ).fetchone()[0]

    print(f"\n{'='*65}")
    print("TAGGING COMPLETE")
    print(f"{'='*65}")
    print(f"\n  Total keyword identities : {total_identities:,}")
    print(f"  Tagged (any method)      : {tagged:,}  "
          f"({tagged/total_identities*100:.1f}%)")

    print(f"\n  Method breakdown:")
    for method, count in sorted(counts.items(), key=lambda x: -x[1]):
        pct = count / total * 100 if total else 0
        print(f"    {method:<20} {count:>8,}  ({pct:.1f}%)")

    print(f"\n  Vertical breakdown:")
    for vert, count in sorted(vertical_counts.items(), key=lambda x: -x[1]):
        pct = count / total * 100 if total else 0
        print(f"    {vert:<20} {count:>8,}  ({pct:.1f}%)")

    # Sample of brand categories
    print(f"\n  Brand category breakdown:")
    rows = cursor.execute("""
        SELECT brand_category, COUNT(*) as cnt
        FROM keyword_identities
        WHERE brand_category IS NOT NULL
        GROUP BY brand_category
        ORDER BY cnt DESC
    """).fetchall()
    for r in rows:
        print(f"    {r[0]:<35} {r[1]:>8,}")

    conn.close()
    print("\nDone. keyword_identities are now tagged.")
    print("Re-run analysis_query_trend.py to see keyword tags in efficiency sheets.")


if __name__ == "__main__":
    main()
