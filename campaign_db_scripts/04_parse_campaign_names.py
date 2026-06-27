"""
04_parse_campaign_names.py
===========================
Campaign Structure Analytics — Home Insurance Google Ads
Folder: C:\\Projects\\google-ads-ops\\campaign_scripts\\

Parses campaign and ad group names to extract segmentation dimensions:

  campaigns:
    - parsed_product_type     (home / renters / condo / flood / unknown)
    - parsed_geo_state        (2-letter US state code, or None)
    - parsed_match_tier       (exact / phrase / broad / mixed / unknown)
    - parsed_intent_tier      (brand / competitor / nonbrand / generic)
    - naming_convention_flag  (1 = name does not parse cleanly; review needed)

  ad_groups:
    - parsed_theme            (cleaned name with common structural tokens stripped)
    - parsed_match_tier
    - parsed_intent_tier
    - naming_convention_flag

This script is intentionally offline — it reads from and writes to the local DB only.
Re-run any time names change, new campaigns are added, or patterns are updated.

IMPORTANT: Review and update the PATTERNS section below to match your actual
naming convention before running. The patterns provided are illustrative examples
based on common paid search conventions and will need to be calibrated to your
account's actual naming scheme.

Run:
  python 04_parse_campaign_names.py

  # Preview what would be flagged without writing to DB
  python 04_parse_campaign_names.py --dry-run

  # Print all parsed results to console
  python 04_parse_campaign_names.py --verbose
"""

import re
import sqlite3
import argparse
import logging
from dataclasses import dataclass

# ── Configuration ─────────────────────────────────────────────────────────────
DB_PATH = r"C:\Projects\google-ads-ops\db\paid_search.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# PATTERNS — UPDATE THESE TO MATCH YOUR ACTUAL NAMING CONVENTION
# ══════════════════════════════════════════════════════════════════════════════
#
# These patterns are applied with re.search() against the full campaign/ad group
# name (case-insensitive). Order within each dict matters for intent_tier:
# the first match wins.
#
# Common naming convention formats this covers:
#   GGL | Home | TX | Exact | NonBrand
#   GGL_Home_CA_Phrase_Brand
#   Home Insurance - Texas - Exact - NonBrand
#   [HO] TX Exact NB
#
# ─────────────────────────────────────────────────────────────────────────────

PRODUCT_PATTERNS: dict[str, re.Pattern] = {
    "home":    re.compile(r"\b(home\s*(?:insurance|ins|owner)?s?|homeowners?|ho3|ho-3)\b", re.I),
    "renters": re.compile(r"\b(renters?\s*(?:insurance|ins)?|ho4|ho-4)\b", re.I),
    "condo":   re.compile(r"\b(condo\s*(?:insurance|ins)?|ho6|ho-6)\b", re.I),
    "flood":   re.compile(r"\b(flood\s*(?:insurance|ins)?|nfip)\b", re.I),
}

# All 50 states + DC, matched as standalone tokens
US_STATES: set[str] = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC",
}
GEO_PATTERN: re.Pattern = re.compile(
    r"(?<![A-Z])(" + "|".join(sorted(US_STATES, key=len, reverse=True)) + r")(?![A-Z])",
    re.I,
)

MATCH_PATTERNS: dict[str, re.Pattern] = {
    "exact":  re.compile(r"\b(exact|em|_e_|[_\-|]ex[_\-|])\b", re.I),
    "phrase": re.compile(r"\b(phrase|pm|_p_|[_\-|]ph[_\-|])\b", re.I),
    "broad":  re.compile(r"\b(broad|bm|_b_|[_\-|]br[_\-|])\b", re.I),
}

# Order matters: brand and competitor are checked before nonbrand/generic
INTENT_PATTERNS: dict[str, re.Pattern] = {
    "brand":      re.compile(r"\b(brand(?:ed)?|_br_|[_\-|]b[_\-|])\b", re.I),
    "competitor": re.compile(r"\b(comp(?:etitor)?s?|oto|conquest|vs\.?|competitor)\b", re.I),
    "nonbrand":   re.compile(r"\b(nb|non[-_\s]?brand|nbt|non[-_\s]?branded)\b", re.I),
}

# Tokens to strip when building parsed_theme for ad groups
THEME_STRIP_PATTERN: re.Pattern = re.compile(
    r"\b(exact|phrase|broad|em|pm|bm|brand(?:ed)?|nb|non[-_\s]?brand|nbt|"
    r"comp(?:etitor)?s?|oto|conquest|GGL|GOOG|Google|Bing|BING|MSN|"
    r"home|renters?|condo|flood|homeowners?|ho[346]|"
    r"(?:" + "|".join(US_STATES) + r")"
    r")\b",
    re.I,
)

# A campaign name is flagged if it fails any of these conditions
MIN_NAME_TOKENS = 3   # names with fewer than this many tokens are suspicious


# ── Data class for parsed results ─────────────────────────────────────────────

@dataclass
class ParsedCampaign:
    product_type:          str
    geo_state:             str | None
    match_tier:            str
    intent_tier:           str
    naming_convention_flag: int
    flag_reasons:          list[str]

@dataclass
class ParsedAdGroup:
    theme:                 str
    match_tier:            str
    intent_tier:           str
    naming_convention_flag: int
    flag_reasons:          list[str]


# ── Parsing logic ──────────────────────────────────────────────────────────────

def parse_campaign(name: str) -> ParsedCampaign:
    reasons = []

    # Product type
    product = next(
        (k for k, p in PRODUCT_PATTERNS.items() if p.search(name)),
        "unknown",
    )
    if product == "unknown":
        reasons.append("no product type token found")

    # Geo state
    geo_match = GEO_PATTERN.search(name)
    geo = geo_match.group(1).upper() if geo_match else None
    # Note: not all campaigns are geo-targeted; absence of state is not always a flag

    # Match tier
    match_hits = [k for k, p in MATCH_PATTERNS.items() if p.search(name)]
    if len(match_hits) == 1:
        match_tier = match_hits[0]
    elif len(match_hits) > 1:
        match_tier = "mixed"
        reasons.append(f"multiple match tier tokens: {match_hits}")
    else:
        match_tier = "unknown"
        reasons.append("no match tier token found")

    # Intent tier
    intent = next(
        (k for k, p in INTENT_PATTERNS.items() if p.search(name)),
        "generic",
    )

    # Token count check
    tokens = [t for t in re.split(r"[\s_\-|]+", name) if t]
    if len(tokens) < MIN_NAME_TOKENS:
        reasons.append(f"name too short ({len(tokens)} tokens)")

    flag = 1 if reasons else 0
    return ParsedCampaign(product, geo, match_tier, intent, flag, reasons)


def parse_ad_group(name: str) -> ParsedAdGroup:
    reasons = []

    # Match tier and intent (same logic as campaign)
    match_hits = [k for k, p in MATCH_PATTERNS.items() if p.search(name)]
    if len(match_hits) == 1:
        match_tier = match_hits[0]
    elif len(match_hits) > 1:
        match_tier = "mixed"
        reasons.append(f"multiple match tier tokens: {match_hits}")
    else:
        match_tier = "unknown"

    intent = next(
        (k for k, p in INTENT_PATTERNS.items() if p.search(name)),
        "generic",
    )

    # Theme: strip structural tokens to reveal the keyword/topic core
    theme = THEME_STRIP_PATTERN.sub("", name)
    theme = re.sub(r"[\s_\-|]+", " ", theme).strip()
    if not theme:
        theme = name   # fall back to full name if nothing remains

    tokens = [t for t in re.split(r"[\s_\-|]+", name) if t]
    if len(tokens) < MIN_NAME_TOKENS:
        reasons.append(f"name too short ({len(tokens)} tokens)")

    flag = 1 if reasons else 0
    return ParsedAdGroup(theme, match_tier, intent, flag, reasons)


# ── DB operations ──────────────────────────────────────────────────────────────

def run_parsing(dry_run: bool = False, verbose: bool = False):
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    # ── Campaigns ──
    cur.execute("SELECT campaign_id, campaign_name FROM campaigns ORDER BY campaign_name")
    campaigns = cur.fetchall()

    campaign_results = []
    for cid, name in campaigns:
        parsed = parse_campaign(name)
        campaign_results.append((cid, name, parsed))
        if verbose or parsed.naming_convention_flag:
            flag_str = f"  ⚠ {'; '.join(parsed.flag_reasons)}" if parsed.flag_reasons else ""
            log.info(
                f"  CAMPAIGN [{cid}] {name!r:60s}  "
                f"product={parsed.product_type:<10} "
                f"geo={str(parsed.geo_state):<5} "
                f"match={parsed.match_tier:<8} "
                f"intent={parsed.intent_tier:<12}"
                f"{flag_str}"
            )

    # ── Ad groups ──
    cur.execute("SELECT ad_group_id, ad_group_name FROM ad_groups ORDER BY ad_group_name")
    ad_groups = cur.fetchall()

    adgroup_results = []
    for agid, name in ad_groups:
        parsed = parse_ad_group(name)
        adgroup_results.append((agid, name, parsed))
        if verbose or parsed.naming_convention_flag:
            flag_str = f"  ⚠ {'; '.join(parsed.flag_reasons)}" if parsed.flag_reasons else ""
            log.info(
                f"  AD GROUP [{agid}] {name!r:60s}  "
                f"theme={parsed.theme!r:<40} "
                f"match={parsed.match_tier:<8} "
                f"intent={parsed.intent_tier:<12}"
                f"{flag_str}"
            )

    # ── Write to DB (unless dry-run) ──
    if not dry_run:
        for cid, _, p in campaign_results:
            cur.execute("""
                UPDATE campaigns
                SET parsed_product_type    = ?,
                    parsed_geo_state       = ?,
                    parsed_match_tier      = ?,
                    parsed_intent_tier     = ?,
                    naming_convention_flag = ?
                WHERE campaign_id = ?
            """, (p.product_type, p.geo_state, p.match_tier, p.intent_tier, p.naming_convention_flag, cid))

        for agid, _, p in adgroup_results:
            cur.execute("""
                UPDATE ad_groups
                SET parsed_theme           = ?,
                    parsed_match_tier      = ?,
                    parsed_intent_tier     = ?,
                    naming_convention_flag = ?
                WHERE ad_group_id = ?
            """, (p.theme, p.match_tier, p.intent_tier, p.naming_convention_flag, agid))

        conn.commit()

    conn.close()

    # ── Summary ──
    n_c_flagged  = sum(1 for _, _, p in campaign_results  if p.naming_convention_flag)
    n_ag_flagged = sum(1 for _, _, p in adgroup_results   if p.naming_convention_flag)

    log.info("")
    log.info("── Parse Summary ─────────────────────────────────────────────")
    log.info(f"  Campaigns  : {len(campaign_results):>5,} parsed  |  {n_c_flagged:>4,} flagged")
    log.info(f"  Ad groups  : {len(adgroup_results):>5,} parsed  |  {n_ag_flagged:>4,} flagged")

    if n_c_flagged > 0:
        log.info(f"")
        log.info(f"  Flagged campaigns (naming_convention_flag = 1):")
        for cid, name, p in campaign_results:
            if p.naming_convention_flag:
                log.info(f"    [{cid}] {name!r}  →  {'; '.join(p.flag_reasons)}")

    if dry_run:
        log.info("")
        log.info("  DRY RUN — no changes written to database.")
    else:
        log.info("")
        log.info("  parsed_* columns updated in campaigns and ad_groups tables.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Parse campaign and ad group names into segmentation dimensions."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report without writing to the database.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print parsed result for every campaign/ad group (not just flagged ones).",
    )
    args = parser.parse_args()

    log.info(f"Parsing campaign and ad group names from: {DB_PATH}")
    if args.dry_run:
        log.info("Mode: DRY RUN (read-only)")
    run_parsing(dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    main()
