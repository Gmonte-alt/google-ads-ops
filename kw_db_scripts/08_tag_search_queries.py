"""
08_tag_search_queries.py
-------------------------
Tags all search queries in search_queries with vertical and modifier
classifications using a four-layer detection pipeline:

    Layer 1  Brand exact match    -- query text in brand_registry_keywords
    Layer 2  Brand fuzzy match    -- Levenshtein distance to brand names
    Layer 3  Pattern match        -- signal lists for vertical, geo, price, coverage
    Layer 4  Claude API           -- residual unclassified queries sent in batches

Results written back to search_queries columns:
    vertical          auto | home | medicare | life | cross | unclassified
    modifier_brand    canonical brand name (normalized)
    modifier_geo      state/city name
    modifier_price    1 if price-intent signal present
    modifier_coverage flood | fire | renters | condo | etc.
    tag_method        brand_exact | brand_fuzzy | pattern | api | unclassified
    tag_confidence    high | medium | low | unclassified

Detection order per query:
    0. Relevance gate  -- does this query relate to insurance at all?
    1. Brand exact     -- normalized query in brand_registry_keywords?
    2. Brand fuzzy     -- edit distance <= threshold to a brand name?
    3. Vertical assign -- which insurance product does the query target?
    4. Modifier check  -- geo, price, coverage applied on top of vertical
    5. API fallback    -- unclassified residual sent to Claude in batches

Usage:
    conda activate paid-search

    Tag all queries (first run or full re-tag):
        python kw_db_scripts/08_tag_search_queries.py --all

    Tag only unclassified queries (incremental after new sync):
        python kw_db_scripts/08_tag_search_queries.py --untagged

    Dry run -- show what would be tagged without writing:
        python kw_db_scripts/08_tag_search_queries.py --all --dry-run

    Skip API layer (faster, leaves residual as unclassified):
        python kw_db_scripts/08_tag_search_queries.py --all --no-api

    Export spot-check sample after tagging:
        python kw_db_scripts/08_tag_search_queries.py --all --spot-check
"""

import sqlite3
import re
import json
import argparse
import os
from pathlib import Path
from datetime import date

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH     = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR  = Path(r"C:\Projects\google-ads-ops\kw_db_scripts\output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

BATCH_SIZE      = 500    # queries per DB commit
API_BATCH_SIZE  = 150    # queries per Claude API call
FUZZY_THRESHOLD = 2      # max edit distance for brand fuzzy match
FUZZY_MIN_LEN   = 4      # minimum query token length to attempt fuzzy match

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

# Note: cross-vertical queries (brand present but no vertical signal in query)
# are intentionally kept as vertical='cross' rather than defaulted to an
# account vertical. The account context is resolved at query time via the
# instance_id -> keyword_instances -> accounts join chain.

# ---------------------------------------------------------------------------
# Signal lists
# ---------------------------------------------------------------------------

# Insurance relevance gate -- query must contain at least one of these
# OR match a brand from the registry to be considered insurance-relevant
INSURANCE_SEED = {
    'insurance', 'insure', 'insured', 'insurer', 'insuring',
    'coverage', 'policy', 'policies', 'premium', 'deductible',
    'claim', 'underwrite', 'underwriting', 'homeowner', 'homeowners',
    'renters', 'medicare', 'medicaid', 'medigap',
}

# Vertical assignment signals -- checked in order; first match wins
# For home/renters, many generic "home" queries are also insurance-relevant
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
        'renters policy',
    ],
    'auto': [
        'auto insurance', 'car insurance', 'vehicle insurance',
        'auto coverage', 'auto policy', 'car policy', 'auto quote',
        'car quote', 'motorcycle insurance', 'driver insurance',
        'driving insurance',
    ],
    'medicare': [
        'medicare', 'medicaid', 'medigap', 'medicare supplement',
        'medicare advantage', 'part a', 'part b', 'medicare plan',
        'senior insurance', 'medicare coverage',
    ],
    'life': [
        'life insurance', 'term life', 'whole life', 'universal life',
        'final expense', 'burial insurance', 'life coverage',
        'life policy', 'life quote',
    ],
}

# Price-intent modifier signals
# Narrowed to terms that specifically signal the user cares about the dollar
# amount -- not general shopping/commercial terms (those go to intent_type/action).
# Removed: 'rate', 'rates', 'quote', 'quotes', 'compare', 'comparison',
#   'cost', 'price', 'pricing', 'estimate', 'get quote', 'online quote',
#   'instant quote', 'free quote', 'best rate'
# (those are captured by intent_type=commercial / intent_action=* instead)
PRICE_SIGNALS = {
    'cheap', 'cheapest', 'affordable', 'low cost', 'inexpensive',
    'how much', 'lowest', 'average cost', 'calculator',
}

# Coverage-type modifier signals -> canonical coverage type
# Covers perils and policy add-ons ONLY.
# Dwelling/home-type signals (condo, mobile home, etc.) moved to HOME_TYPE_SIGNALS.
COVERAGE_SIGNALS = {
    'flood':               'flood',
    'fire insurance':      'fire',
    'fire':                'fire',
    'earthquake':          'earthquake',
    'wildfire':            'wildfire',
    'landlord':            'landlord',
    'rental property':     'landlord',
    'umbrella':            'umbrella',
    'windstorm':           'windstorm',
    'wind':                'wind',
    'hail':                'hail',
    'hurricane':           'hurricane',
    'sinkhole':            'sinkhole',
    'landslide':           'landslide',
    'liability':           'liability',
    'dwelling':            'dwelling',
    # Renters is a policy type, not a home type -- stays here
    'renters insurance':   'renters',
    'renter insurance':    'renters',
    "renter's insurance":  'renters',
    'renters coverage':    'renters',
    'apartment insurance': 'renters',
    'tenant insurance':    'renters',
    'renters policy':      'renters',
    'renters':             'renters',
}

# Home-type modifier signals -> canonical home type
# Captures the dwelling type the user is asking to insure.
# Formerly mixed into COVERAGE_SIGNALS; now a dedicated modifier_home_type column.
HOME_TYPE_SIGNALS = {
    'condo insurance':          'condo',
    'condo coverage':           'condo',
    'condominium insurance':    'condo',
    'condo':                    'condo',
    'condominium':              'condo',
    'mobile home insurance':    'mobile_home',
    'mobile home':              'mobile_home',
    'manufactured home':        'mobile_home',
    'modular home':             'mobile_home',
    'beach house insurance':    'beach_home',
    'beach home insurance':     'beach_home',
    'beach house':              'beach_home',
    'beach home':               'beach_home',
    'vacation home insurance':  'vacation_home',
    'vacation home':            'vacation_home',
    'vacation rental insurance': 'vacation_home',
    'cabin insurance':          'cabin',
    'cabin':                    'cabin',
    'lake house':               'lake_home',
    'lake home':                'lake_home',
    'lakeside home':            'lake_home',
    'townhouse insurance':      'townhouse',
    'townhome insurance':       'townhouse',
    'townhouse':                'townhouse',
    'townhome':                 'townhouse',
    'investment property':      'investment_property',
    'rental property insurance': 'investment_property',
    'second home insurance':    'second_home',
    'second home':              'second_home',
    'duplex insurance':         'duplex',
    'duplex':                   'duplex',
    'tiny home':                'tiny_home',
    'tiny house':               'tiny_home',
}

# Bundle signals -- home + auto package queries
# Queries containing these are flagged with modifier_bundle = 1
# Vertical stays 'cross' since bundle spans both products
BUNDLE_SIGNALS = {
    'home and auto',
    'home & auto',
    'home auto bundle',
    'bundle home',
    'bundle auto',
    'auto and home',
    'auto & home',
    'bundl',          # catches bundle, bundling, bundled
    'multi policy',
    'multi-policy',
    'multipolicy',
    'combo insurance',
    'package policy',
    # Additional patterns for brand + multi-product queries
    'home and car',
    'car and home',
    'home & car',
    'car & home',
    'auto and car',   # edge case
    'home and vehicle',
    'house and auto',
    'house and car',
}

# Near me signals -- hyper-local purchase intent queries
# Kept separate from geo modifier (state/city) because these indicate
# the user wants a local agent/office, not a state-based comparison
NEAR_ME_SIGNALS = {
    'near me',
    'near by',
    'nearby',
    'close to me',
    'in my area',
    'local',          # catches 'local home insurance', 'local agent'
}

# Short brand allowlist -- brand names <= 4 characters that need exact-match
# detection even though they fall below FUZZY_MIN_LEN.
# Loaded dynamically from brand_registry at runtime; this is the fallback set
# in case brand_registry hasn't been seeded yet.
# Rule: matched ONLY when query also passes the insurance relevance gate
# (prevents 'kin' matching 'skin care' etc.)
SHORT_BRAND_FALLBACK = {
    'kin', 'aig', 'aaa', 'qbe', 'aarp',
}

# Brands that are also common English words -- require a qualifying suffix
# in the query to be classified as the brand rather than generic vocabulary.
# Format: {normalized_brand_name: [required_suffix_tokens]}
# NOTE: keep suffixes broad enough to catch home-type variants
# (e.g. "elephant homeowners insurance" must qualify as Elephant the brand)
BRAND_SUFFIX_REQUIRED = {
    'compare.com':  ['compare.com', 'comparethemarket', 'compare the market'],
    'elephant':     ['elephant.com', 'elephant auto', 'elephant insurance',
                     'elephant home', 'elephant homeowners', 'elephant house',
                     'elephant renters'],
    'root':         ['root insurance', 'root auto', 'root.com', 'rootinsurance'],
    'hippo':        ['hippo insurance', 'hippo home', 'hippo.com', 'myhippo'],
    'bamboo':       ['bamboo insurance', 'bamboo home', 'bamboo.com',
                     'bamboo house', 'bamboo ins', 'bamboo homeowners'],
    'mercury':      ['mercury insurance', 'mercury auto', 'mercury.com',
                     'mercury home insurance', 'mercury homeowners',
                     'mercury homeowners insurance', 'mercury house insurance',
                     'mercury fire insurance', 'mercury condo insurance',
                     'mercury home owner'],
    'the general':  ['the general insurance', 'the general auto',
                     'thegeneral.com', 'the general.com'],
}

# Home purchase intent signals -- separate from generic home insurance
# These indicate the user is actively buying a home, not just shopping
# for better rates on an existing policy.
HOME_PURCHASE_SIGNALS = {
    'zillow', 'realtor', 'redfin', 'trulia', 'homes for sale',
    'home for sale', 'buying a home', 'buy a home', 'new home buyer',
    'first time home buyer', 'first-time homebuyer', 'home loan',
    'mortgage rate', 'mortgage payment', 'home purchase', 'escrow',
    'closing costs', 'new construction home', 'for sale by owner',
}

# US states and common abbreviations for geo modifier
GEO_SIGNALS = {
    # Full state names
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
    # High-insurance-relevance cities
    'los angeles','san francisco','san diego','chicago','houston',
    'phoenix','philadelphia','san antonio','dallas','austin',
    'new orleans','miami','tampa','orlando','atlanta',
    'charlotte','raleigh','nashville','denver','seattle',
    'portland','las vegas','phoenix','tucson','albuquerque',
    # Common abbreviations used in insurance searches
    'calif','fla','tex','colo','conn','ariz','tenn','wash',
    'minn','wisc','penn','mass',
    # Standalone state words that appear as brand modifiers
    # (e.g. "aaa texas", "california fair plan", "texas windstorm")
    # These are already in full names above -- adding explicit compound forms
    'aaa texas','aaa colorado','aaa michigan','aaa tidewater',
    'aaa of michigan','aaa of colorado',
}

# Short ambiguous abbreviations that need insurance context to be geo signals
# (won't be applied unless insurance seed term also present)
GEO_AMBIGUOUS = {'ca', 'fl', 'tx', 'ny', 'nj', 'ga', 'va', 'pa', 'nc', 'sc',
                 'la', 'oh', 'mi', 'wa', 'co', 'az', 'il', 'al', 'ky', 'mo'}


# ---------------------------------------------------------------------------
# Intent signals
# Two orthogonal dimensions:
#   intent_type   -- commercial (looking to transact) vs informational (learning)
#   intent_action -- the specific thing the user wants to do/get
#
# These combine with other modifiers for downstream analysis, e.g.:
#   intent_type=commercial + intent_action=compare + modifier_geo  -> high-value
#   intent_type=informational + intent_action=answer + modifier_brand -> brand FAQ
# ---------------------------------------------------------------------------

# intent_type=commercial signals -- user is in buy/compare/quote/transact mode
# Checked first; if any matches, intent_type='commercial'
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
    # Account navigation / product access (existing or new)
    'login', 'log in', 'sign in', 'my account', 'member portal',
    'find a', 'locate a', 'near me',
}

# intent_type=informational signals -- user is learning or researching
# Only applied if no commercial signal fires first
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

# intent_action signals -- ordered list, first match wins.
# navigate is checked first because it is the most disqualifying signal
# for lead-gen (user is managing existing product, not shopping for new one).
INTENT_ACTION_SIGNALS = [
    # navigate -- user is accessing/managing an existing product or account.
    # These queries are typically not convertible for lead-gen and are strong
    # candidates for negative keywords.
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

    Returns:
        (intent_type, intent_action)  -- either or both may be None

    Logic:
      1. Scan INTENT_COMMERCIAL_SIGNALS -> if any hit, intent_type='commercial'
      2. Else scan INTENT_INFORMATIONAL_SIGNALS -> if any hit, intent_type='informational'
      3. Scan INTENT_ACTION_SIGNALS in order -> first match is intent_action
    """
    intent_type   = None
    intent_action = None

    # Step 1: commercial vs informational
    for sig in INTENT_COMMERCIAL_SIGNALS:
        if sig in query:
            intent_type = 'commercial'
            break

    if intent_type is None:
        for sig in INTENT_INFORMATIONAL_SIGNALS:
            if sig in query:
                intent_type = 'informational'
                break

    # Step 2: intent action (independent of intent_type -- action can appear
    # in both commercial and informational contexts)
    for action_tag, signals in INTENT_ACTION_SIGNALS:
        for sig in signals:
            if sig in query:
                intent_action = action_tag
                break
        if intent_action:
            break

    return intent_type, intent_action


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def load_brand_registry(conn) -> tuple[dict, list, list]:
    """
    Returns:
        exact_map         {normalized_keyword: (brand_name, bid_status, verticals, brand_category)}
        compiled_patterns [(compiled_regex, brand_name, bid_status, verticals, brand_category, variant_len)]
                          Sorted longest-variant-first so the first match is the most specific.
                          Pre-compiled once at startup; eliminates per-query re.compile() overhead.
        fuzzy_list        [(normalized_brand_name, brand_name, bid_status, verticals, brand_category)]

    Layer 1 exact/substring matching uses exact_map + compiled_patterns.
    Layer 2 fuzzy matching uses fuzzy_list.
    """
    cursor = conn.cursor()

    # Layer 1: all keyword variants -> brand info
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

    # Pre-compile one regex per variant (word-boundary wrapped), sorted
    # longest-first so the first match in a query is always the most specific.
    # This replaces the inner loop of 2,725 re.search() calls per query with
    # a single compiled pattern scan -- roughly 20-50x faster on large corpora.
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
            pass  # skip any variant that produces an invalid pattern

    # Sort longest variant first so first match = most specific brand name
    compiled_patterns.sort(key=lambda x: x[5], reverse=True)

    # Layer 2: canonical brand names for fuzzy matching
    rows = cursor.execute("""
        SELECT normalized_name, brand_name, bid_status, verticals, brand_category
        FROM brand_registry
        WHERE normalized_name != 'unattributed_negative_keyword'
        ORDER BY LENGTH(normalized_name) DESC
    """).fetchall()

    fuzzy_list = [
        (r['normalized_name'], r['brand_name'],
         r['bid_status'], r['verticals'] or '',
         r['brand_category'] or '')
        for r in rows
    ]

    print(f"  Brand registry loaded: {len(exact_map):,} exact variants "
          f"({len(compiled_patterns):,} compiled patterns), "
          f"{len(fuzzy_list):,} fuzzy candidates")
    return exact_map, compiled_patterns, fuzzy_list


# ---------------------------------------------------------------------------
# Edit distance (Levenshtein)
# ---------------------------------------------------------------------------

def levenshtein(a: str, b: str) -> int:
    """
    Standard dynamic programming Levenshtein edit distance.
    Early exit if row minimum exceeds threshold to avoid wasted computation.
    """
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if len(b) == 0:
        return len(a)

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(
                prev[j + 1] + 1,    # deletion
                curr[j] + 1,        # insertion
                prev[j] + (ca != cb) # substitution
            ))
        prev = curr
    return prev[-1]


def fuzzy_brand_match(
    query_tokens: list[str],
    fuzzy_list: list[tuple],
    threshold: int = FUZZY_THRESHOLD,
    min_len: int = FUZZY_MIN_LEN,
) -> tuple[str, str, str] | None:
    """
    Checks each token in the query against all brand names using edit distance.
    Returns (brand_name, bid_status, verticals) for the best match, or None.

    Only attempts matching on tokens >= min_len characters to avoid false
    positives on short common words. Brand names with multiple tokens
    (e.g. 'liberty mutual') are compared against adjacent token pairs.
    """
    best_brand     = None
    best_status    = None
    best_verticals = None
    best_category  = None
    best_dist      = threshold + 1

    # Single-token brands: compare each query token to each brand
    for norm_brand, brand_name, bid_status, verticals, brand_category in fuzzy_list:
        brand_tokens = norm_brand.split()

        # Skip very short brand names for fuzzy matching -- they cause
        # false positives (e.g. 'esurance' matches any query with 'insurance'
        # because the edit distance is within threshold on the suffix).
        # Short brands are caught by exact match in Layer 1 instead.
        if len(norm_brand) < 6:
            continue

        if len(brand_tokens) == 1:
            brand_tok = brand_tokens[0]
            if len(brand_tok) < min_len:
                continue
            for qtok in query_tokens:
                if len(qtok) < min_len:
                    continue
                if qtok in FUZZY_EXCLUDE_TOKENS:
                    continue
                d = levenshtein(qtok, brand_tok)
                if d < best_dist:
                    best_dist      = d
                    best_brand     = brand_name
                    best_status    = bid_status
                    best_verticals = verticals
                    best_category  = brand_category

        elif len(brand_tokens) == 2:
            # Try matching consecutive token pairs
            for i in range(len(query_tokens) - 1):
                pair = query_tokens[i] + ' ' + query_tokens[i + 1]
                d = levenshtein(pair, norm_brand)
                if d <= threshold and d < best_dist:
                    best_dist      = d
                    best_brand     = brand_name
                    best_status    = bid_status
                    best_verticals = verticals
                    best_category  = brand_category

    if best_brand and best_dist <= threshold:
        return best_brand, best_status, best_verticals, best_category
    return None


# ---------------------------------------------------------------------------
# Vertical inference from brand verticals
# ---------------------------------------------------------------------------

def infer_vertical_from_brand(
    brand_verticals: str, query: str
) -> str:
    """
    Given a brand's verticals string and the query text, determine which
    vertical the query is most likely targeting.

    If brand operates in one vertical, return that vertical.
    If brand is cross-vertical, look for vertical signals in the query.
    If no signal found, return 'cross' -- this is intentionally preserved
    rather than defaulted to the account vertical. The account context
    (Home Insurance) is already known from the instance_id join chain,
    and 'cross' correctly signals that the query intent is ambiguous at
    the keyword level. Useful when expanding to multi-vertical analysis.
    """
    if not brand_verticals:
        return 'unclassified'

    verts = [v.strip() for v in brand_verticals.split(',')]

    if len(verts) == 1:
        # Renters maps to home -- sub-category, not a separate vertical
        return 'home' if verts[0] == 'renters' else verts[0]

    # Cross-vertical brand -- look for vertical signal in query
    for vertical, signals in VERTICAL_SIGNALS.items():
        for sig in signals:
            if sig in query:
                # Renters maps to home
                return 'home' if vertical == 'renters' else vertical

    # No vertical signal in query -- preserve cross for analysis visibility
    return 'cross'


# ---------------------------------------------------------------------------
# Core classification functions
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    return re.sub(r'\s+', ' ', text.lower().strip())


def is_insurance_relevant(query: str, brand_found: bool) -> bool:
    """Relevance gate -- Layer 0."""
    if brand_found:
        return True
    for seed in INSURANCE_SEED:
        if seed in query:
            return True
    return False


def detect_modifiers(query: str) -> dict:
    """
    Detects all modifiers and intent signals from a normalized query.
    Returns dict with all modifier/intent values (None/0 if not present).

    Modifier fields:
        modifier_geo           -- state/city string or None
        modifier_price         -- 1 if true price-intent signal present
        modifier_coverage      -- peril/policy type string or None
        modifier_home_type     -- dwelling type string or None
        modifier_bundle        -- 1 if home+auto bundle signal present
        modifier_near_me       -- 1 if hyper-local intent signal present
        modifier_home_purchase -- 1 if home-buying intent signal present

    Intent fields:
        intent_type   -- 'commercial' | 'informational' | None
        intent_action -- 'compare' | 'quote' | 'answer' | 'checklist' |
                         'review' | 'coverage-lookup' | None
    """
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

    # Bundle detection -- run BEFORE vertical assignment in classify_query
    # so that "[brand] home and car insurance" gets bundle flag even when
    # brand verticals would otherwise resolve to a single vertical.
    for sig in BUNDLE_SIGNALS:
        if sig in query:
            result['modifier_bundle'] = 1
            break

    # Geo -- check full state names first, then ambiguous abbreviations
    for geo_term in GEO_SIGNALS:
        if geo_term in query:
            result['modifier_geo'] = geo_term
            break

    # Ambiguous geo abbreviations only if insurance seed also present
    if result['modifier_geo'] is None:
        has_seed = any(s in query for s in INSURANCE_SEED)
        if has_seed:
            words = query.split()
            for w in words:
                if w in GEO_AMBIGUOUS:
                    result['modifier_geo'] = w
                    break

    # Price signals (true price-intent only -- not commercial/compare signals)
    for sig in PRICE_SIGNALS:
        if sig in query:
            result['modifier_price'] = 1
            break

    # Coverage type (perils/policy types) -- longest match wins
    matched_coverage = None
    matched_len = 0
    for sig, coverage_type in COVERAGE_SIGNALS.items():
        if sig in query and len(sig) > matched_len:
            matched_coverage = coverage_type
            matched_len = len(sig)
    result['modifier_coverage'] = matched_coverage

    # Home type (dwelling type) -- longest match wins
    matched_home_type = None
    matched_ht_len = 0
    for sig, home_type in HOME_TYPE_SIGNALS.items():
        if sig in query and len(sig) > matched_ht_len:
            matched_home_type = home_type
            matched_ht_len = len(sig)
    result['modifier_home_type'] = matched_home_type

    # Near me detection -- hyper-local purchase intent
    for sig in NEAR_ME_SIGNALS:
        if sig in query:
            result['modifier_near_me'] = 1
            break

    # Home purchase intent -- user is buying a home, not rate shopping
    for sig in HOME_PURCHASE_SIGNALS:
        if sig in query:
            result['modifier_home_purchase'] = 1
            break

    # Intent type and action
    result['intent_type'], result['intent_action'] = detect_intent(query)

    return result


def assign_vertical_from_signals(query: str) -> str | None:
    """
    Pattern-based vertical assignment when no brand is present.
    Returns vertical string or None if no signal found.
    """
    for vertical, signals in VERTICAL_SIGNALS.items():
        for sig in signals:
            if sig in query:
                # Renters is a sub-category of home -- map to home vertical
                # and let modifier_coverage carry the renters signal
                return 'home' if vertical == 'renters' else vertical
    return None


def classify_query(
    query_text: str,
    exact_map: dict,
    compiled_patterns: list,
    fuzzy_list: list,
) -> dict:
    """
    Runs the full four-layer classification pipeline on a single query.
    Returns a dict of all classification fields.

    compiled_patterns is pre-built by load_brand_registry() -- passing it in
    avoids re-compiling 2,725 regexes on every single query call.
    """
    q = normalize(query_text)
    tokens = q.split()

    result = {
        'vertical':               None,
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
        'brand_category':         None,
        'tag_method':             'unclassified',
        'tag_confidence':         'unclassified',
    }

    brand_name     = None
    bid_status     = None
    brand_verts    = None
    brand_cat      = None
    brand_found    = False

    # ------------------------------------------------------------------
    # Layer 1: Brand exact match
    # ------------------------------------------------------------------
    if q in exact_map:
        brand_name, bid_status, brand_verts, brand_cat = exact_map[q]
        brand_found = True
        result['tag_method']     = 'brand_exact'
        result['tag_confidence'] = 'high'
    else:
        # Substring scan using pre-compiled word-boundary patterns.
        # Patterns are sorted longest-first, so we can stop at the first match
        # (longest = most specific, e.g. "liberty mutual" beats "liberty").
        for pat, b_name, b_status, b_verts, b_cat, vlen in compiled_patterns:
            if pat.search(q):
                brand_name  = b_name
                bid_status  = b_status
                brand_verts = b_verts
                brand_cat   = b_cat
                brand_found = True
                result['tag_method']     = 'brand_exact'
                result['tag_confidence'] = 'high'
                break  # longest match wins; patterns are pre-sorted

    # ------------------------------------------------------------------
    # Layer 1b: Short brand exact match
    # Brands <= 4 chars are below FUZZY_MIN_LEN so fuzzy won't catch them.
    # Match exactly against individual tokens, but ONLY when the query
    # also passes the insurance relevance gate -- prevents 'kin' matching
    # 'skin care', 'aaa' matching 'aaa battery', etc.
    # ------------------------------------------------------------------
    if not brand_found and is_insurance_relevant(q, False):
        # Short brands from exact_map (single token, <= 4 chars)
        for tok in tokens:
            if len(tok) > 4:
                continue
            # Check exact_map for this token
            if tok in exact_map:
                brand_name, bid_status, brand_verts, brand_cat = exact_map[tok]
                brand_found = True
                result['tag_method']     = 'brand_exact'
                result['tag_confidence'] = 'high'
                break
            # Check SHORT_BRAND_FALLBACK against fuzzy_list
            if tok in SHORT_BRAND_FALLBACK:
                for norm_b, b_name, b_status, b_verts, b_cat in fuzzy_list:
                    if norm_b == tok:
                        brand_name  = b_name
                        bid_status  = b_status
                        brand_verts = b_verts
                        brand_cat   = b_cat
                        brand_found = True
                        result['tag_method']     = 'brand_exact'
                        result['tag_confidence'] = 'high'
                        break
                if brand_found:
                    break

    # ------------------------------------------------------------------
    # Layer 2: Brand fuzzy match (only if no exact match)
    # ------------------------------------------------------------------
    if not brand_found:
        fuzzy_result = fuzzy_brand_match(tokens, fuzzy_list)
        if fuzzy_result:
            brand_name, bid_status, brand_verts, brand_cat = fuzzy_result
            brand_found = True
            result['tag_method']     = 'brand_fuzzy'
            result['tag_confidence'] = 'medium'

    # ------------------------------------------------------------------
    # Relevance gate -- skip unrelated queries
    # ------------------------------------------------------------------
    if not is_insurance_relevant(q, brand_found):
        result['tag_method']     = 'unclassified'
        result['tag_confidence'] = 'unclassified'
        return result

    # ------------------------------------------------------------------
    # Assign brand modifier, category and vertical
    # ------------------------------------------------------------------
    # Brand suffix requirement check
    # Brands that are common English words require a qualifying suffix
    # to be classified as the brand. e.g. 'compare' alone should not
    # match compare.com -- require 'compare.com' or 'comparethemarket'.
    # ------------------------------------------------------------------
    if brand_found:
        brand_norm_check = normalize(brand_name)
        if brand_norm_check in BRAND_SUFFIX_REQUIRED:
            suffixes = BRAND_SUFFIX_REQUIRED[brand_norm_check]
            if not any(sfx in q for sfx in suffixes):
                brand_found   = False
                brand_name    = None
                bid_status    = None
                brand_verts   = None
                brand_cat     = None
                result['tag_method']     = 'unclassified'
                result['tag_confidence'] = 'unclassified'

    if brand_found:
        result['modifier_brand']  = normalize(brand_name)
        result['brand_category']  = brand_cat or None
        result['vertical'] = infer_vertical_from_brand(brand_verts, q)
        # Downgrade confidence if brand verticals ambiguous
        if result['vertical'] == 'cross':
            result['tag_confidence'] = 'medium'

    # ------------------------------------------------------------------
    # Layer 3: Pattern-based vertical assignment
    # (applies if no brand found, or brand is cross-vertical)
    # ------------------------------------------------------------------
    if result['vertical'] is None or result['vertical'] == 'cross':
        pattern_vertical = assign_vertical_from_signals(q)
        if pattern_vertical:
            result['vertical'] = pattern_vertical
            if not brand_found:
                result['tag_method']     = 'pattern'
                result['tag_confidence'] = 'medium'
        elif not brand_found:
            # Has insurance relevance but no vertical signal
            result['vertical']       = 'unclassified'
            result['tag_method']     = 'pattern'
            result['tag_confidence'] = 'low'

    # ------------------------------------------------------------------
    # Modifier detection -- applies to all queries.
    # Bundle check is inside detect_modifiers and runs first so it can
    # be inspected here before we finalise vertical.
    # ------------------------------------------------------------------
    modifiers = detect_modifiers(q)
    result.update(modifiers)

    # Bundle override -- if query contains a bundle signal (home + auto),
    # promote vertical to 'cross' regardless of what brand/pattern found.
    # This fixes e.g. "costco home and car insurance" which was resolving
    # to vertical=home because the brand's home vertical was set first.
    if result['modifier_bundle']:
        result['vertical'] = 'cross'
        if not brand_found:
            result['tag_method']     = 'pattern'
            result['tag_confidence'] = 'medium'

    # If vertical still unclassified but a coverage type was detected,
    # infer home vertical (flood/fire/earthquake are home perils).
    # Also check home_type -- condo/mobile_home queries are home vertical.
    if result['vertical'] in (None, 'unclassified'):
        if result['modifier_coverage'] or result['modifier_home_type']:
            result['vertical'] = 'home'
            if not brand_found:
                result['tag_method']     = 'pattern'
                result['tag_confidence'] = 'medium'

    # Ensure queries that got here have a tag_method
    if result['tag_method'] == 'unclassified' and (
        brand_found or result['vertical'] not in (None, 'unclassified')
    ):
        if not brand_found:
            result['tag_method'] = 'pattern'

    return result


# ---------------------------------------------------------------------------
# API layer (Layer 4) -- Claude API for residual unclassified queries
# ---------------------------------------------------------------------------

TAXONOMY_PROMPT = """You are classifying insurance search queries for a lead generation business.
The business operates in four insurance verticals: home, auto, medicare, life.

For each query, return a JSON object with:
  "vertical": one of "home", "auto", "medicare", "life", "cross", "unclassified"
  "tag_confidence": one of "high", "medium", "low"

Rules:
- "cross" means the query could apply to multiple verticals
- "unclassified" means the query is not insurance-relevant or too ambiguous
- Home includes renters/dwelling/property insurance
- Medicare includes medigap, supplement plans, senior health coverage
- Life includes term, whole, final expense, burial insurance
- Do NOT include health insurance (not a vertical we serve)

Return ONLY a JSON array with one object per query, in the same order.
Example output for 3 queries:
[{"vertical":"home","tag_confidence":"high"},{"vertical":"auto","tag_confidence":"medium"},{"vertical":"unclassified","tag_confidence":"low"}]"""


def classify_via_api(queries: list[str]) -> list[dict]:
    """
    Sends a batch of queries to the Claude API for vertical classification.
    Returns list of {vertical, tag_confidence} dicts in same order as input.
    Falls back to 'unclassified' on any error.
    """
    try:
        import urllib.request
        import urllib.error

        numbered = "\n".join(f"{i+1}. {q}" for i, q in enumerate(queries))
        payload = json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1000,
            "messages": [{
                "role": "user",
                "content": f"{TAXONOMY_PROMPT}\n\nClassify these queries:\n{numbered}"
            }]
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())

        text = "".join(
            item["text"] for item in data.get("content", [])
            if item.get("type") == "text"
        )
        # Extract JSON array
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            results = json.loads(match.group())
            if len(results) == len(queries):
                return results

    except Exception as e:
        print(f"    API error: {e}")

    # Fallback -- return unclassified for all
    return [{"vertical": "unclassified", "tag_confidence": "low"}] * len(queries)


# ---------------------------------------------------------------------------
# Main tagging loop
# ---------------------------------------------------------------------------

def run_tagger(conn, tag_all: bool, dry_run: bool, use_api: bool):
    cursor = conn.cursor()

    # Load brand registry into memory
    print("Loading brand registry...")
    exact_map, compiled_patterns, fuzzy_list = load_brand_registry(conn)

    # Fetch queries to tag
    if tag_all:
        rows = cursor.execute(
            "SELECT id, query_text, normalized_text FROM search_queries"
        ).fetchall()
        print(f"Tagging ALL {len(rows):,} queries")
    else:
        rows = cursor.execute(
            """SELECT id, query_text, normalized_text FROM search_queries
               WHERE tag_method = 'unclassified' OR tag_method IS NULL"""
        ).fetchall()
        print(f"Tagging {len(rows):,} unclassified queries")

    total = len(rows)

    # Classification counters
    counts = {
        'brand_exact': 0, 'brand_fuzzy': 0,
        'pattern': 0, 'api': 0, 'unclassified': 0,
    }
    vertical_counts = {}
    api_batch = []
    api_batch_ids = []
    updates = []

    import time
    t_start    = time.time()
    t_last     = t_start
    REPORT_N   = 1000   # print progress every N rows

    print()  # blank line before progress output

    for i, row in enumerate(rows):
        qid    = row['id']
        q_text = row['query_text']

        result = classify_query(q_text, exact_map, compiled_patterns, fuzzy_list)
        counts[result['tag_method']] = counts.get(result['tag_method'], 0) + 1
        vertical_counts[result['vertical'] or 'none'] = \
            vertical_counts.get(result['vertical'] or 'none', 0) + 1

        # Queue for API if still unclassified and API enabled
        if use_api and result['tag_method'] == 'unclassified':
            api_batch.append(q_text)
            api_batch_ids.append((qid, result))
            if len(api_batch) >= API_BATCH_SIZE:
                api_results = classify_via_api(api_batch)
                for (aid, ares), api_r in zip(api_batch_ids, api_results):
                    ares['vertical']       = api_r.get('vertical', 'unclassified')
                    ares['tag_confidence'] = api_r.get('tag_confidence', 'low')
                    ares['tag_method']     = 'api'
                    counts['api']          += 1
                    counts['unclassified'] -= 1
                    updates.append((ares, aid))
                api_batch.clear()
                api_batch_ids.clear()
        else:
            updates.append((result, qid))

        # Commit in batches
        if not dry_run and len(updates) >= BATCH_SIZE:
            _flush_updates(cursor, updates)
            conn.commit()
            updates.clear()

        # Inline progress with ETA -- overwrites the same terminal line
        done = i + 1
        if done % REPORT_N == 0 or done == total:
            now       = time.time()
            elapsed   = now - t_start
            rate      = done / elapsed if elapsed > 0 else 0
            remaining = (total - done) / rate if rate > 0 else 0
            pct       = done / total * 100

            # Format ETA as m:ss or h:mm:ss
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
                f"{rate:,.0f} q/s  "
                f"ETA {eta_str}  "
                f"exact={counts['brand_exact']:,} "
                f"fuzzy={counts['brand_fuzzy']:,} "
                f"pattern={counts['pattern']:,} "
                f"unclass={counts['unclassified']:,}"
            )
            print(line, end='', flush=True)

    print()  # newline after progress bar completes

    # Flush remaining API batch
    if use_api and api_batch:
        api_results = classify_via_api(api_batch)
        for (aid, ares), api_r in zip(api_batch_ids, api_results):
            ares['vertical']       = api_r.get('vertical', 'unclassified')
            ares['tag_confidence'] = api_r.get('tag_confidence', 'low')
            ares['tag_method']     = 'api'
            counts['api']          += 1
            counts['unclassified'] -= 1
            updates.append((ares, aid))

    # Final flush
    if not dry_run and updates:
        _flush_updates(cursor, updates)
        conn.commit()

    elapsed_total = time.time() - t_start
    print(f"  Finished {total:,} queries in "
          f"{int(elapsed_total//60)}m{int(elapsed_total%60):02d}s  "
          f"({total/elapsed_total:,.0f} q/s avg)")

    return counts, vertical_counts


def _flush_updates(cursor, updates: list):
    cursor.executemany("""
        UPDATE search_queries SET
            vertical               = ?,
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
            brand_category         = ?,
            tag_method             = ?,
            tag_confidence         = ?
        WHERE id = ?
    """, [
        (
            r['vertical'], r['modifier_brand'], r['modifier_geo'],
            r['modifier_price'], r['modifier_coverage'],
            r.get('modifier_home_type'),
            r.get('modifier_bundle', 0),
            r.get('modifier_near_me', 0),
            r.get('modifier_home_purchase', 0),
            r.get('intent_type'),
            r.get('intent_action'),
            r.get('brand_category'),
            r['tag_method'], r['tag_confidence'],
            qid,
        )
        for r, qid in updates
    ])


# ---------------------------------------------------------------------------
# Spot-check export
# ---------------------------------------------------------------------------

def export_spot_check(conn):
    """
    Exports a stratified sample for manual review:
    - Top 20 by revenue per tag_method
    - 50 random from each vertical
    Output: kw_db_scripts/output/tag_spot_check.csv
    """
    import csv

    output_path = OUTPUT_DIR / "tag_spot_check.csv"
    cursor = conn.cursor()

    rows = cursor.execute("""
        SELECT
            sq.query_text,
            sq.vertical,
            sq.brand_category,
            sq.modifier_brand,
            sq.modifier_geo,
            sq.modifier_price,
            sq.modifier_coverage,
            sq.modifier_home_type,
            sq.modifier_bundle,
            sq.modifier_near_me,
            sq.intent_type,
            sq.intent_action,
            sq.tag_method,
            sq.tag_confidence,
            COALESCE(SUM(sqp.primary_conv_value), 0) AS revenue,
            COALESCE(SUM(sqp.clicks), 0)             AS clicks
        FROM search_queries sq
        LEFT JOIN search_query_performance sqp
            ON sqp.query_id = sq.id AND sqp.device = 'ALL'
        GROUP BY sq.id
        ORDER BY revenue DESC, clicks DESC
        LIMIT 2000
    """).fetchall()

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'query_text', 'vertical', 'brand_category', 'modifier_brand',
            'modifier_geo', 'modifier_price', 'modifier_coverage',
            'modifier_home_type', 'modifier_bundle', 'modifier_near_me',
            'intent_type', 'intent_action',
            'tag_method', 'tag_confidence',
            'revenue', 'clicks'
        ])
        for r in rows:
            writer.writerow(list(r))

    print(f"\nSpot-check exported: {output_path}")
    print(f"  {len(rows):,} rows (top 2,000 by revenue)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Tag search queries with vertical and modifier classifications"
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--all", action="store_true",
        help="Tag all queries (full re-tag)"
    )
    mode_group.add_argument(
        "--untagged", action="store_true",
        help="Tag only unclassified queries (incremental)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show classification results without writing to database"
    )
    parser.add_argument(
        "--no-api", action="store_true",
        help="Skip API layer -- leave residual queries as unclassified"
    )
    parser.add_argument(
        "--spot-check", action="store_true",
        help="Export spot-check CSV after tagging"
    )
    args = parser.parse_args()

    use_api = not args.no_api

    print("=" * 65)
    print("08_tag_search_queries.py")
    mode_str = "ALL" if args.all else "UNTAGGED ONLY"
    print(f"Mode     : {mode_str}"
          f"{' (DRY RUN)' if args.dry_run else ''}"
          f"{' (NO API)' if not use_api else ''}")
    print(f"Date     : {date.today().isoformat()}")
    print("=" * 65)

    conn = get_conn()

    counts, vertical_counts = run_tagger(
        conn,
        tag_all=args.all,
        dry_run=args.dry_run,
        use_api=use_api,
    )

    total = sum(counts.values())
    print(f"\n{'='*65}")
    print("TAGGING COMPLETE")
    print(f"{'='*65}")
    print(f"\n  Classification method breakdown:")
    for method, count in sorted(counts.items(), key=lambda x: -x[1]):
        pct = count / total * 100 if total > 0 else 0
        print(f"    {method:<20} {count:>8,}  ({pct:>5.1f}%)")

    print(f"\n  Vertical breakdown:")
    for vert, count in sorted(vertical_counts.items(), key=lambda x: -x[1]):
        pct = count / total * 100 if total > 0 else 0
        print(f"    {vert:<20} {count:>8,}  ({pct:>5.1f}%)")

    if not args.dry_run and args.spot_check:
        export_spot_check(conn)

    conn.close()
    print("\nDone.")
    if args.dry_run:
        print("NOTE: Dry run -- no changes written to database.")
        print("Re-run without --dry-run to apply tags.")


if __name__ == "__main__":
    main()
