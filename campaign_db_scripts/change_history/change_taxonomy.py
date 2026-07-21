"""
change_taxonomy.py -- controlled vocabulary + prose categorizer for the weekly management change log.

Pure stdlib. Turns a change-history "Changes" prose cell (Google UI grid, already in
google_change_csv_raw.changes_raw) into structured records:
    {category, grain, action, klass, count, subtype}
where klass = 'restructure' | 'modification' per the confirmed rules:
  * whole campaign / ad-group status change  -> restructure
  * single keyword / ad status change        -> modification
  * geo re-tiering (regions moved), bid-strategy swap, campaign/ad-group create -> restructure
  * budget / bid-target value, keyword add, RSA new/paused, negatives -> modification

Dimensions live elsewhere (resolved per change, not in this module):
  * business_unit -> from the ACCOUNT (Home for account 1)
  * vertical      -> campaign-name STARTSWITH prefix (derive_vertical); stored by campaign_id
  * carrier|generic -> added-keyword text matched to brand_registry (is_home_carrier); needs live DB
"""
import re

# ---------- vertical: business_unit = account; vertical = campaign-name prefix ----------
# startswith test (NOT delimiter split -- COMBINED shells & a possible HOME INSURANCE-RENTERS-
# both split to "HOME INSURANCE"). Data-driven: add ("RENTERS INSURANCE","renters") at launch.
PREFIX_VERTICAL = [
    ("HOME INSURANCE", "home"),
    # ("RENTERS INSURANCE", "renters"),   # add when renters campaigns launch (name-derivable, confirmed)
    # ("COMMERCIAL", "commercial"),        # if/when commercial campaigns exist
]

def derive_vertical(campaign_name, default="home"):
    nm = (campaign_name or "").strip().upper()
    for pfx, vert in PREFIX_VERTICAL:
        if nm.startswith(pfx):
            return vert
    return default

# ---------- carrier classification (per brand_registry, architect-confirmed) ----------
def is_home_carrier(brand_category, verticals):
    """carrier = brand_category LIKE 'carrier_%'; home-relevant = verticals LIKE '%home%'.
    The keyword->brand match itself happens in the enricher against live brand_registry."""
    return bool(brand_category and str(brand_category).startswith("carrier_")
                and verticals and "home" in str(verticals).lower())

# ---------- classes ----------
R, M = "restructure", "modification"

def _norm(s):
    return (s or "").replace("\u202f", " ").replace("\xa0", " ")

_LEAD_INT = re.compile(r"^\s*(\d+)\b")
def _count(text, default=1):
    """Only a LEADING count ('5 regions added' -> 5). Single-entity changes
    ('Campaign changed', 'Ad group created', 'Label added') -> 1. Never read an
    integer from mid-prose (percentages, dollar amounts, dates, tier numbers)."""
    m = _LEAD_INT.match(text)
    return int(m.group(1)) if m else default

# ---------- category patterns (ordered: most specific first, first match wins) ----------
# (regex, category, grain, action, klass)   -- regex tested against the full normalized prose
_PATTERNS = [
    # --- bidding ---
    (r"moved from bid strategy named",                 "bid_strategy_swap",   "campaign", "swap",    R),
    (r"['\"][^'\"]*combined['\"]:\s*target roas changed", "bid_target_portfolio", "strategy", "update", M),
    (r"ad group target roas (changed|increased|decreased|updated)", "bid_target_adgroup", "ad_group", "update", M),
    (r"target roas changed from",                      "bid_target_campaign", "campaign", "update",  M),
    # --- budget ---
    (r"budget amount (increased|decreased|changed)",   "budget_change",       "campaign", "update",  M),
    (r"\bbudget created\b",                             "budget_created",      "campaign", "create",  R),
    (r"budget name changed",                            "budget_rename",       "campaign", "rename",  M),
    # --- geo / criteria ---
    (r"\d+\s+negative (region|cities|city)",            "geo_negative",        "campaign", "add",     M),
    (r"\d+\s+regions?\s+added",                         "geo_retiering",       "campaign", "add",     R),
    (r"\d+\s+regions?\s+removed",                       "geo_retiering",       "campaign", "remove",  R),
    # --- keywords ---
    (r"\d+\s+negative\s+\w*\s*match\s+keywords?\s+(added|removed)", "negative_keyword", "ad_group", "addremove", M),
    (r"negative keyword list .* (applied|removed)",     "negative_list",       "campaign", "apply",   M),
    (r"\d+\s+\w*\s*match\s+keywords?\s+added",           "keyword_add",         "ad_group", "add",     M),
    (r"\d+\s+\w*\s*match\s+keywords?\s+paused",          "keyword_status",      "keyword",  "pause",   M),
    (r"\d+\s+\w*\s*match\s+keywords?\s+removed",         "keyword_status",      "keyword",  "remove",  M),
    # --- ads (RSA) ---
    (r"responsive search ad created",                   "rsa_new",             "ad_group", "create",  M),
    (r"responsive search ad changed.*status changed .* to paused", "rsa_paused", "ad_group", "pause",  M),
    (r"responsive search ad changed",                   "rsa_change",          "ad_group", "update",  M),
    # --- structural status ---
    (r"ad group created",                               "adgroup_created",     "ad_group", "create",  R),
    (r"\d+\s+ad groups?\s+(paused|enabled|removed)",    "adgroup_status",      "ad_group", "status",  R),
    (r"\d+\s+campaigns?\s+(active|paused|removed)",     "campaign_status",     "campaign", "status",  R),
    (r"status changed from (paused|removed) to active", "campaign_status",     "campaign", "status",  R),
    # --- assignment / config (lower priority; kept for completeness) ---
    (r"tracking template changed",                      "tracking_template",   "campaign", "update",  M),
    (r"campaign asset created",                         "asset_change",        "campaign", "create",  M),
    (r"label added",                                    "label_change",        "campaign", "label",   M),
]
_COMPILED = [(re.compile(rx, re.I | re.S), cat, gr, act, kl) for rx, cat, gr, act, kl in _PATTERNS]

def categorize(changes_raw, campaign_col=None, ad_group_col=None):
    """Return a single categorized record (first pattern match) or an 'other' record.
    One grid row = one logical change, so we classify the whole cell to one category."""
    text = _norm(changes_raw)
    for rx, cat, grain, action, klass in _COMPILED:
        if rx.search(text):
            # context rule (#3): a status change scoped to a single keyword/ad is a modification,
            # a whole campaign/ad-group is a restructure -- already encoded per-pattern above.
            return {"category": cat, "grain": grain, "action": action,
                    "klass": klass, "count": _count(text), "subtype": None}
    return {"category": "other", "grain": None, "action": None,
            "klass": M, "count": 1, "subtype": (text.split("\n")[0][:40] or None)}
