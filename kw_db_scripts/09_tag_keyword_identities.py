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
    'home and car', 'car and home', 'home & car', 'car & home',
    'home and vehicle', 'house and auto', 'house and car',
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
    'how much', 'lowest', 'calculator',
}

COVERAGE_SIGNALS = {
    'flood': 'flood', 'fire insurance': 'fire', 'fire': 'fire',
    'earthquake': 'earthquake', 'wildfire': 'wildfire',
    'landlord': 'landlord', 'rental property': 'landlord',
    'umbrella': 'umbrella', 'windstorm': 'windstorm',
    'wind': 'wind', 'hail': 'hail', 'hurricane': 'hurricane',
    'sinkhole': 'sinkhole', 'landslide': 'landslide',
    'liability': 'liability', 'dwelling': 'dwelling',
    # Renters is a policy type, not a home type -- stays here
    'renters insurance': 'renters', 'renter insurance': 'renters',
    'renters': 'renters',
}

# Home-type modifier signals -> canonical home type
# Captures the dwelling type the user is asking to insure.
HOME_TYPE_SIGNALS = {
    'condo insurance':           'condo',
    'condo coverage':            'condo',
    'condominium insurance':     'condo',
    'condo':                     'condo',
    'condominium':               'condo',
    'mobile home insurance':     'mobile_home',
    'mobile home':               'mobile_home',
    'manufactured home':         'mobile_home',
    'modular home':              'mobile_home',
    'beach house insurance':     'beach_home',
    'beach home insurance':      'beach_home',
    'beach house':               'beach_home',
    'beach home':                'beach_home',
    'vacation home insurance':   'vacation_home',
    'vacation home':             'vacation_home',
    'vacation rental insurance': 'vacation_home',
    'cabin insurance':           'cabin',
    'cabin':                     'cabin',
    'lake house':                'lake_home',
    'lake home':                 'lake_home',
    'lakeside home':             'lake_home',
    'townhouse insurance':       'townhouse',
    'townhome insurance':        'townhouse',
    'townhouse':                 'townhouse',
    'townhome':                  'townhouse',
    'investment property':       'investment_property',
    'rental property insurance': 'investment_property',
    'second home insurance':     'second_home',
    'second home':               'second_home',
    'duplex insurance':          'duplex',
    'duplex':                    'duplex',
    'tiny home':                 'tiny_home',
    'tiny house':                'tiny_home',
}

FUZZY_THRESHOLD = 2
FUZZY_MIN_LEN   = 6

# Tokens that must never be fuzzy-matched to a brand name.
FUZZY_EXCLUDE_TOKENS = {
    'insurance', 'insure', 'insured', 'insurer', 'coverage',
    'homeowners', 'homeowner', 'renters', 'renter', 'policy',
    'policies', 'premium', 'claim', 'quotes', 'quote',
    'auto', 'home', 'life', 'health', 'medicare', 'medicaid',
    'mortgage', 'general', 'mutual', 'national', 'american',
}
BATCH_SIZE = 500

# ---------------------------------------------------------------------------
# Intent signals (mirrors 08_tag_search_queries.py)
# intent_type   -- commercial | informational
# intent_action -- compare | quote | answer | checklist | review | coverage-lookup
# ---------------------------------------------------------------------------

INTENT_COMMERCIAL_SIGNALS = {
    # Quoting / pricing
    'quote', 'quotes', 'get a quote', 'free quote', 'online quote',
    'instant quote', 'get quote',
    # Comparing
    'compare', 'comparison', 'compare rates', 'compare plans',
    'alternatives', 'options',
    # Quality / ranking (commercial investigation)
    'best', 'top', 'top rated', 'best rated', 'highest rated',
    'ranked', 'ranking', 'recommended',
    # Price-driven
    'cheapest', 'cheap', 'affordable', 'lowest', 'low cost',
    'save', 'save on', 'save money', 'discount',
    # Transaction actions
    'switch', 'switching', 'cancel', 'buy', 'purchase',
    'sign up', 'enroll', 'enrollment', 'apply', 'get covered',
    'start a policy', 'get a policy', 'get insured',
    # Shopping signals
    'find', 'shop', 'shopping', 'looking for',
    # Account navigation / product access
    'login', 'log in', 'sign in', 'my account', 'member portal',
    'find a', 'locate a', 'near me',
}

INTENT_INFORMATIONAL_SIGNALS = {
    # Question starters
    'what is', 'what are', 'what does', 'what do',
    'how does', 'how do', 'how to', 'how much',
    'should i', 'can i', 'is there', 'is it', 'will my', 'will it',
    'why', 'when', 'which', 'who covers', 'who is',
    # Explanation / learning
    'explained', 'learn', 'understand', 'understanding',
    'meaning', 'definition', 'overview',
    # Average / typical
    'average', 'typical', 'usually', 'normally', 'generally',
    # Requirements
    'required', 'require', 'requirement', 'mandatory',
    'do i need', 'do you need',
    # Difference / comparison (research mode)
    'difference between', 'difference',
    # Reviews / reputation research
    'review', 'reviews', 'rated', 'rating', 'ratings',
    'complaint', 'complaints', 'bbb', 'jd power', 'am best',
    'worth it', 'reliable', 'trustworthy',
    # Coverage research
    'covered', 'not covered', 'exclusion', 'exclusions',
    'what covers', 'does cover', 'does it cover',
    'types of', 'kinds of',
}

INTENT_ACTION_SIGNALS = [
    # navigate -- user is accessing/managing an existing product or account.
    # Not shopping for new coverage -- strong negative keyword candidates.
    ('navigate',        ['login', 'log in', 'sign in', 'my account',
                         'member portal', 'member login', 'online account',
                         'pay my bill', 'pay bill', 'make a payment',
                         'file a claim', 'claims', 'report a claim',
                         'find a doctor', 'find a pharmacy', 'find a provider',
                         'find an agent', 'find an office', 'find a location',
                         'phone number', 'contact number', 'customer service number',
                         '800 number', 'call us',
                         'enrollment', 'enroll now', 'enroll online',
                         'sign up for', 'apply for', 'register',
                         'renew my', 'cancel my', 'update my',
                         'download app', 'mobile app']),
    # compare -- wants side-by-side options or rankings
    ('compare',         ['compare', 'comparison', 'vs', 'versus',
                         'compare rates', 'compare plans', 'compare companies',
                         'best rates', 'shop around', 'alternatives',
                         'which is better', 'top rated', 'best rated',
                         'ranked', 'ranking', 'best home insurance',
                         'best homeowners insurance', 'best renters insurance']),
    # quote -- wants a price or estimate
    ('quote',           ['quote', 'quotes', 'get a quote', 'free quote',
                         'online quote', 'instant quote', 'get quote',
                         'estimate', 'calculator', 'how much',
                         'how much does', 'how much is', 'how much for',
                         'cost', 'price', 'pricing', 'rates', 'rate',
                         'average cost', 'average price', 'monthly cost',
                         'annual cost', 'per month', 'per year']),
    # answer -- specific yes/no or factual question about a brand or product
    ('answer',          ['does', 'do they', 'does geico', 'does state farm',
                         'does allstate', 'does progressive', 'does farmers',
                         'does nationwide', 'does usaa', 'does lemonade',
                         'does hippo', 'does kin', 'does bamboo',
                         'does costco', 'does aaa', 'does aarp',
                         'is there', 'can i get', 'will my', 'will it',
                         'is it covered', 'does cover', 'do they cover',
                         'does it cover', 'do i need', 'is required',
                         'is mandatory']),
    # checklist -- wants steps, requirements, or a process
    ('checklist',       ['how to get', 'how do i get', 'what do i need',
                         'what do you need', 'requirements', 'required',
                         'steps to', 'process', 'checklist',
                         'what to look for', 'how to find', 'how to choose',
                         'tips', 'what to know', 'guide', 'what to expect',
                         'how to apply', 'how to buy', 'how to switch',
                         'how to cancel', 'how to file']),
    # review -- researching brand reputation or quality
    ('review',          ['review', 'reviews', 'ratings', 'rating', 'rated',
                         'complaints', 'complaint', 'customer service',
                         'am best', 'jd power', 'bbb', 'trustpilot',
                         'worth it', 'reliable', 'trustworthy',
                         'pros and cons', 'pros cons', 'is it good',
                         'is it worth', 'is it legit', 'legitimate']),
    # coverage-lookup -- researching what a policy covers or excludes
    ('coverage-lookup', ['what does cover', 'what is covered', 'covered',
                         'not covered', 'exclusion', 'exclusions', 'included',
                         'policy covers', 'what covers', 'coverage for',
                         'does it include', 'types of coverage',
                         'coverage options', 'what is covered by',
                         'what is not covered', 'coverage limits',
                         'deductible', 'claim']),
]


def detect_intent(query: str) -> tuple[str | None, str | None]:
    """
    Detects intent_type and intent_action from a normalized query string.
    Returns (intent_type, intent_action) -- either or both may be None.
    """
    intent_type   = None
    intent_action = None

    for sig in INTENT_COMMERCIAL_SIGNALS:
        if sig in query:
            intent_type = 'commercial'
            break

    if intent_type is None:
        for sig in INTENT_INFORMATIONAL_SIGNALS:
            if sig in query:
                intent_type = 'informational'
                break

    for action_tag, signals in INTENT_ACTION_SIGNALS:
        for sig in signals:
            if sig in query:
                intent_action = action_tag
                break
        if intent_action:
            break

    return intent_type, intent_action


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
        ("vertical",               "TEXT"),
        ("brand_category",         "TEXT"),
        ("modifier_brand",         "TEXT"),
        ("modifier_geo",           "TEXT"),
        ("modifier_price",         "INTEGER DEFAULT 0"),
        ("modifier_coverage",      "TEXT"),
        ("modifier_home_type",     "TEXT"),
        ("modifier_bundle",        "INTEGER DEFAULT 0"),
        ("modifier_near_me",       "INTEGER DEFAULT 0"),
        ("modifier_home_purchase", "INTEGER DEFAULT 0"),
        ("intent_type",            "TEXT"),
        ("intent_action",          "TEXT"),
        ("tag_method",             "TEXT DEFAULT 'unclassified'"),
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

    # Pre-compile word-boundary patterns, sorted longest-first.
    # Eliminates per-keyword re.compile() overhead (~2,725 patterns x N keywords).
    SUBSTR_MIN_LEN = 3
    compiled_patterns = []
    for variant, (b_name, b_status, b_verts, b_cat) in exact_map.items():
        if not variant or len(variant) < SUBSTR_MIN_LEN:
            continue
        try:
            pat = re.compile(
                r'(?<![a-z])' + re.escape(variant) + r'(?![a-z])'
            )
            compiled_patterns.append(
                (pat, b_name, b_status, b_verts, b_cat, len(variant))
            )
        except re.error:
            pass
    compiled_patterns.sort(key=lambda x: x[5], reverse=True)

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
    print(f"  Brand registry: {len(exact_map):,} exact variants "
          f"({len(compiled_patterns):,} compiled patterns), "
          f"{len(fuzzy_candidates):,} fuzzy candidates")
    return exact_map, compiled_patterns, fuzzy_candidates

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
        'modifier_home_type':     None,
        'modifier_bundle':        0,
        'modifier_near_me':       0,
        'modifier_home_purchase': 0,
        'intent_type':            None,
        'intent_action':          None,
    }
    # Bundle first -- so classify_keyword can inspect it before finalising vertical
    for sig in BUNDLE_SIGNALS:
        if sig in query:
            result['modifier_bundle'] = 1
            break
    for geo in GEO_SIGNALS:
        if geo in query:
            result['modifier_geo'] = geo
            break
    for sig in PRICE_SIGNALS:
        if sig in query:
            result['modifier_price'] = 1
            break
    # Coverage type -- longest match wins
    matched_cov, matched_len = None, 0
    for sig, cov_type in COVERAGE_SIGNALS.items():
        if sig in query and len(sig) > matched_len:
            matched_cov = cov_type
            matched_len = len(sig)
    result['modifier_coverage'] = matched_cov
    # Home type -- longest match wins
    matched_ht, matched_ht_len = None, 0
    for sig, home_type in HOME_TYPE_SIGNALS.items():
        if sig in query and len(sig) > matched_ht_len:
            matched_ht     = home_type
            matched_ht_len = len(sig)
    result['modifier_home_type'] = matched_ht
    for sig in NEAR_ME_SIGNALS:
        if sig in query:
            result['modifier_near_me'] = 1
            break
    for sig in HOME_PURCHASE_SIGNALS:
        if sig in query:
            result['modifier_home_purchase'] = 1
            break
    result['intent_type'], result['intent_action'] = detect_intent(query)
    return result


def classify_keyword(kw_text: str, match_type: str,
                     exact_map: dict, compiled_patterns: list,
                     fuzzy_list: list) -> dict:
    """
    Classify a keyword identity using the same pipeline as the query tagger.
    Keyword text is cleaner than search queries so exact match rates are higher.
    compiled_patterns is pre-built by load_brand_registry() for performance.
    """
    q = normalize(kw_text)
    # Strip broad match modifier syntax (+keyword)
    q_clean = q.replace('+', ' ')
    q_clean = re.sub(r'\s+', ' ', q_clean).strip()
    tokens  = q_clean.split()

    result = {
        'vertical':               None,
        'brand_category':         None,
        'modifier_brand':         None,
        'modifier_geo':           None,
        'modifier_price':         0,
        'modifier_coverage':      None,
        'modifier_home_type':     None,
        'modifier_bundle':        0,
        'modifier_near_me':       0,
        'modifier_home_purchase': 0,
        'intent_type':            None,
        'intent_action':          None,
        'tag_method':             'unclassified',
    }

    brand_name = bid_status = brand_verts = brand_cat = None
    brand_found = False

    # Layer 1: exact full-string match
    if q_clean in exact_map:
        brand_name, bid_status, brand_verts, brand_cat = exact_map[q_clean]
        brand_found = True
        result['tag_method'] = 'brand_exact'
    else:
        # Substring scan using pre-compiled word-boundary patterns.
        # Sorted longest-first: first match = most specific brand.
        for pat, b_name, b_status, b_verts, b_cat, vlen in compiled_patterns:
            if pat.search(q_clean):
                brand_name  = b_name
                bid_status  = b_status
                brand_verts = b_verts
                brand_cat   = b_cat
                brand_found = True
                result['tag_method'] = 'brand_exact'
                break  # longest match wins; patterns are pre-sorted

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
            result['vertical']   = pattern_vertical
            result['tag_method'] = 'pattern'
        else:
            result['vertical']   = 'unclassified'
            result['tag_method'] = 'unclassified'

    # Modifiers apply to all keywords (bundle detection runs inside here first)
    modifiers = detect_modifiers(q_clean)
    result.update(modifiers)

    # Bundle override -- if keyword contains a home+auto bundle signal,
    # promote vertical to 'cross' regardless of brand/pattern resolution.
    if result['modifier_bundle']:
        result['vertical'] = 'cross'
        if not brand_found:
            result['tag_method'] = 'pattern'

    # Coverage or home_type can disambiguate unclassified non-brand keywords
    if result['vertical'] in (None, 'unclassified'):
        if result['modifier_coverage'] or result['modifier_home_type']:
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
    exact_map, compiled_patterns, fuzzy_list = load_brand_registry(conn)

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

    import time
    t_start  = time.time()
    REPORT_N = 500   # print progress every N rows (keywords are faster than queries)

    print()  # blank line before progress

    for i, row in enumerate(identities):
        kid        = row['id']
        kw_text    = row['normalized_text']
        match_type = row['match_type']

        result = classify_keyword(kw_text, match_type, exact_map, compiled_patterns, fuzzy_list)
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
            result.get('modifier_home_type'),
            result['modifier_bundle'],
            result.get('modifier_near_me', 0),
            result.get('modifier_home_purchase', 0),
            result.get('intent_type'),
            result.get('intent_action'),
            result['tag_method'],
            kid,
        ))

        if len(updates) >= BATCH_SIZE:
            _flush(cursor, updates)
            conn.commit()
            updates.clear()

        done = i + 1
        if done % REPORT_N == 0 or done == total:
            elapsed   = time.time() - t_start
            rate      = done / elapsed if elapsed > 0 else 0
            remaining = (total - done) / rate if rate > 0 else 0
            pct       = done / total * 100

            if remaining >= 3600:
                eta_str = f"{int(remaining//3600)}h{int((remaining%3600)//60):02d}m"
            else:
                eta_str = f"{int(remaining//60)}m{int(remaining%60):02d}s"

            bar_width = 30
            filled    = int(bar_width * done / total)
            bar       = '█' * filled + '░' * (bar_width - filled)

            line = (
                f"\r  [{bar}] {pct:5.1f}%  "
                f"{done:>{len(str(total))},}/{total:,}  "
                f"{rate:,.0f} kw/s  "
                f"ETA {eta_str}  "
                f"exact={counts['brand_exact']:,} "
                f"fuzzy={counts.get('brand_fuzzy',0):,} "
                f"pattern={counts.get('pattern',0):,} "
                f"unclass={counts['unclassified']:,}"
            )
            print(line, end='', flush=True)

    print()  # newline after progress bar

    if updates:
        _flush(cursor, updates)
        conn.commit()

    elapsed_total = time.time() - t_start
    print(f"  Finished {total:,} keywords in "
          f"{int(elapsed_total//60)}m{int(elapsed_total%60):02d}s  "
          f"({total/elapsed_total:,.0f} kw/s avg)")

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
            modifier_home_type     = ?,
            modifier_bundle        = ?,
            modifier_near_me       = ?,
            modifier_home_purchase = ?,
            intent_type            = ?,
            intent_action          = ?,
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
