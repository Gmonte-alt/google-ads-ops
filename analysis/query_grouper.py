#!/usr/bin/env python
"""
query_grouper.py -- VERBATIM extraction of the V3 query-group taxonomy from
analysis parity_common.py (query_group + its regex battery + is_navigate).

Extracted so the query-share classification and the parity workbook group
queries IDENTICALLY -- same rule as brand_matcher-from-09: one canonical
implementation, extracted not reimplemented. If parity_common.py's
query_group changes, RE-EXTRACT this file; do not let them drift.

Usage:
    from query_grouper import query_group
    label = query_group("state farm home insurance texas", brand=True)
    # -> "3. Brand + geo"
`brand` = does the trusted registry match a brand in this query.
"""
import re

NAVIGATE_RE = re.compile(
    r"\b(log\s?in|log\s?on|sign\s?in|bill\s?pay|pay (my |the )?bill|make a payment|claim status|"
    r"file a claim|customer service|phone number|cancel (my )?policy|my account|account login|"
    r"member(s)? login|policy login|payment portal)\b")


def is_navigate(text):
    return bool(NAVIGATE_RE.search((text or "").lower()))


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
