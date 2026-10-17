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

_CNT_KW = re.compile(r"(\d+)\s+(?:negative\s+)?(?:\w+\s+)?match\s+keyword", re.I)
_CNT_OTHER = re.compile(r"(\d+)\s+(?:negative\s+)?(regions?|cities|ad groups?|campaigns?)", re.I)
_LEAD_INT = re.compile(r"^\s*(\d+)\b")
def _count(text, default=1):
    """Item count. Handles leading ('5 regions added'), the count before the noun
    ('...Confie: 92 negative phrase match keywords added'), and single-entity changes -> 1.
    Never reads a mid-prose integer (percent, dollar, date, tier number)."""
    for rx in (_CNT_KW, _CNT_OTHER):
        m = rx.search(text)
        if m:
            return int(m.group(1))
    m = _LEAD_INT.match(text)
    return int(m.group(1)) if m else default

# ---------- category patterns (ordered: most specific first, first match wins) ----------
# (regex, category, grain, action, klass)   -- regex tested against the full normalized prose
_PATTERNS = [
    # --- bidding ---
    (r"moved from bid strategy named",                 "bid_strategy_swap",   "campaign", "swap",    R),
    (r"['\"][^'\"]+['\"]:\s*(?:min\.?\s+|target\s+)roas changed", "bid_target_portfolio", "strategy", "update", M),
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
    (r"responsive search ads? removed",                 "rsa_removed",         "ad_group", "remove",  M),
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
    # --- keyword enable (status; enabled was previously uncaught) ---
    (r"\d+\s+\w*\s*match\s+keywords?\s+enabled",         "keyword_status",      "keyword",  "enable",  M),
    # --- PMax / audience / targeting (2025+ vocabulary; PMax in scope) ---
    (r"assets? in asset group .* created",              "pmax_asset_group",    "asset_group", "create", R),
    (r"asset group .* (created|added)",                 "pmax_asset_group",    "asset_group", "create", R),
    (r"\d+\s+audience segments?\s+(added|removed)",      "audience_targeting",  "campaign", "add",     M),
    (r"\d+\s+detailed demographics?\s+(added|removed)",  "audience_targeting",  "campaign", "add",     M),
    (r"\d+\s+languages?\s+(added|removed)",              "language_targeting",  "campaign", "add",     M),
    (r"\d+\s+platforms?\s+(added|removed)",              "platform_targeting",  "campaign", "add",     M),
    (r"\d+\s+negative (countries|country|metros?|regions?|cities|city)\s+added",
                                                        "geo_negative",        "campaign", "add",     M),
    (r"\d+\s+negative content labels?\s+added",          "content_exclusion",   "campaign", "add",     M),
    (r"\d+\s+ad schedules?\s+(added|removed|changed)",  "ad_schedule",         "campaign", "update",  M),
    (r"seasonality adjustment .* (updated|created|named|changed)",
                                                        "seasonality_adj",     "account",  "update",  M),
    (r"(standard goals changed|account-default goals)",  "conversion_goals",    "campaign", "update",  M),
    (r"(campaign |ad group |customer )?asset (created|changed|removed)",
                                                        "asset_change",        "campaign", "update",  M),
    (r"negative brand lists?\s+added",                   "negative_list",       "campaign", "apply",   M),
    (r"\bcampaign created\b",                            "campaign_created",    "campaign", "create",  R),
    # --- GENERIC header fallbacks: MUST stay last & header-anchored (^) so detail-specific
    #     patterns above win; these catch only otherwise-unclassified 'X changed' headers ---
    (r"^budget changed",                                 "budget_change",       "campaign", "update",  M),
    (r"^campaign changed",                               "campaign_config",     "campaign", "update",  M),
    (r"^ad group changed",                               "adgroup_config",      "ad_group", "update",  M),
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


def split_blocks(changes_raw):
    """Split a Changes cell into sub-change blocks. Real cells bundle several changes
    (e.g. exact + phrase + broad keyword-add blocks, or a keyword-add plus a label and
    RSA-path change). A block = one UNINDENTED header line plus the INDENTED detail lines
    beneath it (Google indents details by 2 spaces)."""
    text = _norm(changes_raw)
    blocks, cur = [], []
    for ln in text.split("\n"):
        if not ln.strip():
            continue
        if not ln[:1].isspace():          # unindented -> starts a new block
            if cur:
                blocks.append("\n".join(cur))
            cur = [ln]
        else:
            cur.append(ln)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def categorize_cell(changes_raw, campaign_col=None, ad_group_col=None):
    """Categorize EACH sub-change block -> list of records. A multi-change cell yields
    multiple records (so counts by category are per-action, not per-cell)."""
    return [categorize(b, campaign_col, ad_group_col) for b in split_blocks(changes_raw)]
