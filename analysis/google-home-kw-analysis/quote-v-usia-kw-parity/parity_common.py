#!/usr/bin/env python
"""
parity_common.py -- shared primitives for the Quote.com <-> USIA keyword parity analysis.

Single source of truth for the cross-cutting operations, so the 13_ theme-persist pass and the
three-set join apply identical logic ("same function both sides -> comparable by construction"):

  covering_key(text)     -- whitespace-collapsing match key for the covering check (see note below)
  categorize(text)       -- spec taxonomy: ADJACENT bucket, then BUNDLE / HOMEOWNERS_EXT, then
                            assign_theme (12 themes | CORE). Imported from usia_keyword_select so
                            it cannot drift from the USIA side.
  make_brand_tagger(db)  -- BrandTagger wired to the live registry with the two-pronged sentinel
                            guard (drop unattributed_negative_keyword rows; skip empty/punct-only
                            variant keys that would poison self.exact[''] via last-writer-wins).
  brand_of(tagger, text) -- brand tag for a surface string (normalize_for_match + embedded fallback).
  load_usia_targets(csv) -- Set 1 universe: exclusion-gated USIA generic candidates, categorized.

Read-only against paid_search.db. Build/validate on synthetic; run on live and paste output.
Conventions (carry verbatim): scope account_id=1 via keyword_instances (sqp has no account_id);
device='ALL'; cost = cost_micros/1e6; primary_conv_count/value as truth metrics.

NOTE -- these import usia_keyword_select (which imports sklearn) and brand_tagger. Place this file
alongside those two so the imports resolve in the paid-search env.
"""
import csv
import os
import re
import sqlite3
import sys

# usia_keyword_select.py and brand_tagger.py live in the kw-planner tree, NOT next to this file.
# Put that dir on sys.path so we import the LIVE selector/tagger directly -- no duplicated copies,
# so categorize() cannot drift from the USIA side. Override with USIA_SCRIPTS_DIR if the layout moves.
USIA_SCRIPTS_DIR = os.environ.get(
    "USIA_SCRIPTS_DIR", r"C:\Projects\google-ads-ops\kw-planner\home_insurance")
if os.path.isdir(USIA_SCRIPTS_DIR) and USIA_SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, USIA_SCRIPTS_DIR)

# Standard project paths (used by this module's self-check and by the downstream join/deliverables).
# LIVE_DB_PATH is a human reference ONLY -- deliberately never wired as an argparse default, so no
# run can silently target production. Every script must name its --db explicitly.
LIVE_DB_PATH = r"C:\Projects\google-ads-ops\db\paid_search.db"  # reference only; never a default
DEFAULT_DEMAND_DB = r"C:\Projects\google-ads-ops\db\demand_research.db"  # read-only source
DEFAULT_OUT_DIR = (r"C:\Projects\google-ads-ops\analysis\google-home-kw-analysis"
                   r"\quote-v-usia-kw-parity\output")

# NOTE: if this import fails, either USIA_SCRIPTS_DIR is wrong, or the live selector is a different
# file (e.g. usia_keyword_select_v2.py). categorize() must import from whichever file you actually
# run to build USIA, or the two sides stop being comparable by construction.
from usia_keyword_select import (
    assign_theme, ADJACENT_RE, is_bundle, is_extension, clean_keyword,
    USIA_OUT_PRODUCTS,
)
from brand_tagger import BrandTagger, normalize_for_match


# --- covering-check normalizer ----------------------------------------------------------------
# The covering check answers "does this SQP query exist as a keyword instance (any match type)".
# It is a MATCH decision, NOT a reproduction of search_queries.normalized_text. The stored column
# was written by 05's `query_text.lower().strip()`, which PRESERVES internal double-spaces, while
# keyword_instances.keyword_text is single-spaced (Google Ads normalizes keywords). Comparing a
# single-spaced keyword against a double-space-preserving stored value under-matches (false
# "not covered") regardless of which normalizer is applied to the keyword side -- you cannot
# re-introduce a lost double-space. So the match key COLLAPSES internal whitespace on BOTH sides:
# re-normalize query_text fresh here rather than leaning on the stored 05 column. Collapsing only
# removes redundant whitespace; it never merges semantically-distinct queries, so it introduces no
# false positives. (The query_id -> search_queries join for target_theme inheritance is unaffected;
# that is an integer-FK join, not a string match.)
def covering_key(text):
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text.lower().strip())


# --- categorization (spec taxonomy) -----------------------------------------------------------
# Generic home-insurance head terms (bare "home insurance", "homeowners insurance companies",
# "home insurance by address") carry no intent modifier, so assign_theme drops them to CORE. That
# is the single biggest reason CORE swamped the live theme map (~81% of realized value). GENERIC_HOME
# reclaims exactly that residual: applied ONLY when assign_theme already returned CORE AND the text
# is recognizably home-insurance, so it never steals from a real theme. Same function both sides.
_HOME_ROOT_RE = re.compile(r"\b(home\s?owners?|homeowners?|home|house|dwelling)\b")
_INS_SIGNAL_RE = re.compile(r"insur|\bhoi\b|\bho-?3\b|\bins\b|policy|coverage|quote|premium")

# Account-management / navigation intent (login, bill pay, claim status). Never a good add or
# re-enable -- these are existing-customer service queries, not acquisition demand.
NAVIGATE_RE = re.compile(
    r"\b(log\s?in|log\s?on|sign\s?in|bill\s?pay|pay (my |the )?bill|make a payment|claim status|"
    r"file a claim|customer service|phone number|cancel (my )?policy|my account|account login|"
    r"member(s)? login|policy login|payment portal)\b")


def is_navigate(text):
    return bool(NAVIGATE_RE.search((text or "").lower()))


def categorize(text):
    """ADJACENT bucket -> BUNDLE -> HOMEOWNERS_EXT -> assign_theme (12 themes) -> GENERIC_HOME (home
    head terms that would otherwise be CORE) -> CORE. Spec taxonomy plus the GENERIC_HOME split.
    Narrower than the full USIA classify_exclusion by design (navigate/geo/carrier stay in themes;
    navigate is handled as an OUTPUT filter, not a category)."""
    t = (text or "").lower()
    for name, rx in ADJACENT_RE:
        if rx.search(t):
            return "ADJACENT:" + name
    if is_bundle(text):
        return "BUNDLE"
    if is_extension(text):
        return "HOMEOWNERS_EXT"
    theme = assign_theme(text)
    if theme == "CORE" and _HOME_ROOT_RE.search(t) and _INS_SIGNAL_RE.search(t):
        return "GENERIC_HOME"
    return theme


# Theme buckets that should not be surfaced as recommended ADDS in the keyword drill (they still
# get a target_theme and still appear in the theme map; they are just not "port these to Quote.com"
# candidates). ADJACENT:* is prefix-matched.
# --- V3-style query-group taxonomy (trusted reconstruction) -----------------------------------
# Reconstructs the human-readable Query Groups from home_insurance_keyword_analysisV3.py with NO
# dependency on 08's intent_action / is_unclassified gating (which we fenced). Brand comes from the
# trusted registry (caller passes brand=True/False); everything else is regex over query text.
# Specific-intent groups V3 drove off 08 (reviews / coverage-lookup / checklist / quote) are
# reconstructed as trusted token regex and given priority ABOVE the generic brand buckets, so those
# distinctions surface (V3 relied on 08's is_unclassified gate to route them; we can't). Returns
# V3's exact label strings for direct comparability with the V3 workbook.
_QG_STATE = re.compile(r"\b(?:alabama|alaska|arizona|arkansas|california|colorado|connecticut|"
    r"delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|"
    r"maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|"
    r"new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|"
    r"pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|"
    r"washington|west virginia|wisconsin|wyoming)\b")
_QG_CA_CITY = re.compile(r"\b(?:bay area|sunnyvale|elk grove|encino|pacific palisades|truckee|"
    r"sacramento|bakersfield|riverside|irvine|santa barbara|modesto|palm springs|temecula|carlsbad|"
    r"marin county|north hollywood|rancho cucamonga|san luis obispo|big bear|lake arrowhead|"
    r"mammoth lakes|south lake tahoe|burbank|sherman oaks|san jose|san mateo|san francisco|"
    r"los angeles|san diego|long beach|fresno|stockton|anaheim|santa ana|chula vista|fremont|"
    r"glendale|huntington beach|santa clara|garden grove|oceanside|ontario|oxnard|moreno valley|"
    r"fontana|santa rosa|pomona|torrance|escondido|salinas)\b")
_QG_PRODUCT = re.compile(r"\b(insurance|homeowners|home insurance|homeowners insurance|home owner)\b")
_QG_HOMEOWNERS = re.compile(r"\b(homeowners|home owners|home owner|homeowner)\b")
_QG_FLOOD = re.compile(r"\b(nfip|flood|inundaci|elevation certificate|floodsimple|rocketflood|"
    r"catcoverage|cat coverage|aon edge|beyond floods|tsunami|tfia|neptune|shore one|floodplain|"
    r"windstorm|wind storm|wind and hail|hail insurance|hurricane insurance|tornado insurance|"
    r"wind insurance|wind damage insurance|wind mitigation|named storm|named windstorm|twia|twic|"
    r"earthquake insurance|earthquake authority|earthquakeauthority|seismic insurance|ca earthquake|"
    r"cea earthquake|cea policy|hazard insurance|hazard coverage|hazard policy|catastrophic insurance|"
    r"catastrophe insurance|catastrophic homeowners|named peril|sinkhole insurance|landslide insurance|"
    r"mudslide insurance|disaster insurance)\b")
_QG_FIRE = re.compile(r"\b(fair act|fair plan|cal fair|calfair|california fair|ca fire|fire hazard|"
    r"fire risk|fire zone|hazard zone|zone zero|cfp net|fire insurance|wildfire insurance|"
    r"wild fire insurance|brush fire insurance|forest fire insurance|fire damage insurance|"
    r"fire coverage)\b")
_QG_DISTRESS = re.compile(r"\b(cancelled|canceled|dropped|non.?renewal|lapses?|lapse|drops me|"
    r"drops you|non standard|non-standard|dropped me|dropped by|getting dropped|being dropped)\b")
_QG_HIGHRISK = re.compile(r"\b(high.?risk|knob and tube|dog bite|old roof|denied|can.?t get|"
    r"houses over 100|100 years|non standard)\b")
_QG_COST = re.compile(r"\b(average|how much|annual|inflation guard|cheapest|cost of|how much does|"
    r"price of|average cost|what.*cost|what.*price|what.*average)\b")
_QG_COMPARE = re.compile(r"\b(compare|best|who has the best|top rated|highest rated)\b")
_QG_VS = re.compile(r"\b(vs\.?|versus)\b")
_QG_DOES = re.compile(r"\bdoes\b.{0,40}\b(insurance|home|homeowners|house|insure|cover)\b")
_QG_REVIEW = re.compile(r"\breviews?\b|\brating(s)?\b|\bcomplaints?\b|\bbbb\b")
_QG_COVLOOKUP = re.compile(r"\bwhat (does|is)\b.*\b(cover|covered|coverage)\b|\bis\b.*\bcovered\b|"
    r"\bdoes\b.*\bcover\b|\bcoverage for\b|\bwhat is covered\b|\bdeductibles?\b|\bclaims?\b|"
    r"\bexclusions?\b|\bbuy ?back\b|\bafter a claim\b")
_QG_CHECKLIST = re.compile(r"\bchecklist\b|\bhow (to|do i)\b|\bguide\b|\bstep by step\b|"
    r"\bwhat do i need\b|\bhow much .* do i need\b|\brequire(d|ment|ments)?\b|\brequired by law\b")
_QG_QUOTE = re.compile(r"\b(quote|quotes|free quote|instant quote|online quote)\b")


def query_group(text, brand=False):
    """V3-style Query Group label for a query. `brand` = does the trusted registry match a brand.
    Priority recovers V3's distinctions: peril/situation -> specific intents -> geo -> brand
    structure -> generic -> quote/navigate. Returns V3's exact label strings."""
    t = (text or "").lower().strip()
    wc = len(t.split())
    # peril / situation (regardless of brand or intent)
    if _QG_FLOOD.search(t):    return "9. Flood, wind & hazard insurance"
    if _QG_FIRE.search(t):     return "8. Fire insurance & FAIR plan"
    if _QG_DISTRESS.search(t): return "6. Policy distress / cancellation"
    if _QG_HIGHRISK.search(t): return "7. High risk / hard to insure"
    # specific intents (priority above generic brand buckets so they surface)
    if brand and _QG_REVIEW.search(t):             return "12. Carrier reviews"
    if _QG_DOES.search(t):                          return "14. Does + carrier + have"
    if _QG_COVLOOKUP.search(t):                     return "Classified: Coverage lookup"
    if _QG_CHECKLIST.search(t):                     return "Classified: Checklist / how-to"
    if _QG_COMPARE.search(t) and not _QG_VS.search(t): return "13. Compare / best"
    if (not brand) and _QG_COST.search(t):         return "10. Cost / pricing informational"
    # geo
    if brand and _QG_STATE.search(t):              return "3. Brand + geo"
    if (not brand) and _QG_CA_CITY.search(t):      return "11. Local geo search"
    # brand structure
    if brand and wc <= 2:                          return "2. Pure brand name"
    if brand and _QG_PRODUCT.search(t) and wc <= 4: return "1. Brand + product name"
    if brand:                                      return "5. Brand direct (other)"
    # generic / quote / navigate
    if _QG_HOMEOWNERS.search(t):                   return "4. Generic homeowners (no brand)"
    if _QG_QUOTE.search(t):
        return "Classified: Quote intent (geo)" if _QG_STATE.search(t) else "Classified: Quote intent"
    if is_navigate(t):                             return "Classified: Navigate (existing customer)"
    return "Unclassified"


# --- output-filter helpers --------------------------------------------------------------------
SUPPRESS_ADD_PREFIXES = ("ADJACENT:",)


def is_suppressed_add(theme):
    return any(theme.startswith(p) for p in SUPPRESS_ADD_PREFIXES)


# --- brand tagging with the two-pronged sentinel guard ----------------------------------------
SENTINEL_BRAND = "unattributed_negative_keyword"


class GuardedBrandTagger(BrandTagger):
    """BrandTagger whose registry load excludes the unattributed_negative_keyword sentinel and any
    empty / punctuation-only variant key. Prong 1: the sentinel brand is filtered at the
    brand_registry read, so its 6,970 negative-keyword artifact variants (phone numbers,
    "phrase"/[exact] surfaces) never enter self.exact and cannot mis-tag real brands on the clean
    collisions (liberty / statefarm / compare com). Prong 2: variant keys with no [a-z0-9] are
    skipped, so self.exact[''] is never populated (last-writer-wins poisoning)."""

    def _load_registry(self, path):
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        id_to_norm = {}
        for r in cur.execute(
            "SELECT id, brand_name, normalized_name, bid_status, brand_category, verticals "
            "FROM brand_registry WHERE normalized_name != ?", (SENTINEL_BRAND,)
        ):
            self.meta[r["normalized_name"]] = (r["brand_name"], r["bid_status"],
                                               r["brand_category"], r["verticals"])
            id_to_norm[r["id"]] = r["normalized_name"]
        for r in cur.execute("SELECT brand_id, normalized FROM brand_registry_keywords"):
            nm = id_to_norm.get(r["brand_id"])           # sentinel brand_id absent -> nm is None -> skip
            key = (r["normalized"] or "")
            if nm and re.search(r"[a-z0-9]", key.lower()):  # prong 2: skip empty / punct-only keys
                self.exact[key] = nm
        conn.close()


def make_brand_tagger(db_path):
    return GuardedBrandTagger(db_path)


def brand_of(tagger, text):
    """(brand, bid_status, category, dnb_flag, source) for a surface string. normalize_for_match
    reproduces ~74.5% of stored keys exactly; the remainder resolve via BrandTagger's embedded
    (canonical-name, len>=4) fallback. Empty brand => non-brand."""
    return tagger.tag(normalize_for_match(text))


# --- Set 1: exclusion-gated USIA candidate universe -------------------------------------------
# The live selector's load_candidates gate + classify_exclusion's OUT-product knockout, reproduced
# from the v2 export. product_code is used as an EXCLUSION of USIA_OUT_PRODUCTS -- NEVER as a
# positive inclusion gate (an inclusion gate drops ~39% of the base, incl. the empty-product_code
# generic heads like "home owners insurance quote"). Empty product_code is retained and routed by
# categorize(). Approximation vs live: the export lacks product_ambiguous, so we cannot honor
# classify_exclusion's `if not ambiguous` carve-out; we exclude ALL OUT-product rows (~26-row /
# ~0.6% over-exclusion, accepted for v1). vertical='home' is enforced here because the export is
# full-vertical (still carries renters rows) despite its label.
def load_usia_targets(csv_path):
    """Return (by_theme, flat): by_theme = {theme: [(keyword, vol), ...]},
    flat = [(keyword, theme, vol), ...] deduped by clean_keyword (max volume)."""
    agg = {}  # clean_keyword -> max vol
    with open(csv_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["vertical"] != "home":
                continue
            if r["geo_name"] != "United States":
                continue
            if r["vertical_match"] != "on_vertical":
                continue
            if r["dnb_flag"] != "0":
                continue
            if (r["brand"] or "") != "":
                continue
            try:
                vol = int(float(r["avg_monthly_searches"] or 0))
            except ValueError:
                vol = 0
            if vol <= 0:
                continue
            if (r["product_code"] or "") in USIA_OUT_PRODUCTS:   # exclusion knockout (approx)
                continue
            ck = clean_keyword(r["keyword_text"])
            if not ck:
                continue
            agg[ck] = max(agg.get(ck, 0), vol)

    by_theme, flat = {}, []
    for kw, vol in sorted(agg.items(), key=lambda x: -x[1]):
        theme = categorize(kw)
        by_theme.setdefault(theme, []).append((kw, vol))
        flat.append((kw, theme, vol))
    return by_theme, flat


def load_usia_brand_targets(csv_path):
    """USIA carrier/conquest targets from usia_brand_select's companion CSV
    (columns: campaign, ad_group, brand, keyword, match_type, volume, cluster_members).
    These are brand/carrier by construction. Returns (by_theme, flat) where flat rows are dicts."""
    by_theme, flat, seen = {}, [], set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            kw = (r.get("keyword") or "").strip()
            if not kw:
                continue
            ck = covering_key(kw)
            if ck in seen:
                continue
            seen.add(ck)
            try:
                vol = int(float(r.get("volume") or 0))
            except ValueError:
                vol = 0
            theme = categorize(kw)
            by_theme.setdefault(theme, []).append((kw, vol))
            flat.append(dict(keyword=kw, theme=theme, volume=vol,
                             brand=(r.get("brand") or "").strip(), group="BRAND", source="carrier"))
    return by_theme, flat


def load_usia_all(generic_csv, brand_csv=None):
    """Unified USIA target universe: generic (non-brand) + carrier (brand). Returns (by_theme, flat)
    with flat rows as dicts {keyword, theme, volume, brand, group, source}."""
    g_by_theme, g_flat = load_usia_targets(generic_csv)
    by_theme = {t: list(v) for t, v in g_by_theme.items()}
    flat = [dict(keyword=k, theme=t, volume=v, brand="", group="NONBRAND", source="generic")
            for (k, t, v) in g_flat]
    seen = {covering_key(r["keyword"]) for r in flat}
    if brand_csv and os.path.exists(brand_csv):
        _, b_flat = load_usia_brand_targets(brand_csv)
        for r in b_flat:
            if covering_key(r["keyword"]) in seen:
                continue
            seen.add(covering_key(r["keyword"]))
            flat.append(r)
            by_theme.setdefault(r["theme"], []).append((r["keyword"], r["volume"]))
    return by_theme, flat


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="parity_common self-check against the v2 export")
    p.add_argument("--csv", default="kwp_ideas_export_v2.csv")
    a = p.parse_args()
    by_theme, flat = load_usia_targets(a.csv)
    print("USIA target universe: %d keywords across %d themes" % (len(flat), len(by_theme)))
    for theme in sorted(by_theme, key=lambda t: -len(by_theme[t])):
        n = len(by_theme[theme])
        vol = sum(v for _, v in by_theme[theme])
        print("  %-24s %4d kw   vol=%s" % (theme, n, f"{vol:,}"))
