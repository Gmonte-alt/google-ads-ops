#!/usr/bin/env python3
"""
Google Home Insurance - Bulk Import Generator for Ads Editor (RSAs)

Produces a single CSV combining:
  1. PAUSE rows for every active in-scope RSA in the account (preserves history)
  2. CREATE rows for new RSAs assembled per the locked creative design

INTENDED PATH
-------------
    C:\\Projects\\google-ads-ops\\ad_assets\\exports\\bulk_import_ads_google.py

INPUTS
------
  --db-path        SQLite database (paid_search.db) -- for ad_group_id, campaign_id lookups
  --workbook       rsa-load-ready-copy_vN.xlsx     -- copy library (UB, intent sets, swaps)
  --existing-rsas  Ad_report_google_YYYY-MM-DD.csv -- current RSAs to pause + URL/path inheritance

OUTPUTS
-------
  bulk_import_ads_<account_slug>_<YYYY-MM-DD>.csv         -- combined pause + create CSV
  bulk_import_ads_<account_slug>_<YYYY-MM-DD>_report.txt  -- validation + assembly report

DESIGN
------
The locked creative design lives in LOGICAL_AD_GROUP_DESIGN + TIER_RULES below.
Copy text is sourced from the workbook:
  - "ub:N"               -- Universal Backbone sheet, headline/desc row N (1-indexed)
  - "is:N" / "is:all"    -- Intent set within RSA Ad-Group Load Asset sheet
  - "swaps:<text>"       -- Headline Swaps sheet ADD section, by exact text match

CONVENTIONS
-----------
- Read-only against paid_search.db (no INSERT / UPDATE / DELETE)
- Active-status-hierarchy filtering only (no last_seen_date)
- Account filter uses integer accounts.id (not platform_account_id string)
- Final URL / paths / tracking inherited from existing RSAs; hard-fail on conflict
- Pinning honored exactly from workbook (all Unpinned per current design)
- Location insertion syntax normalized to {Location(State):Your State} form
- Carrier substitution applied per ad-group from LOGICAL_AD_GROUP_DESIGN
- Character limits validated post-substitution; hard-fail on violation
- Windows UTF-8 stdout reconfigure for safe piping

v11 COPY ENGINE (Carrier Variant Ladders + Build Script Rules)
--------------------------------------------------------------
- Brand-token ([Carrier]) headlines resolve through the workbook's Carrier
  Variant Ladders sheet: Home P1 -> Home P2 -> bare rung -> drop, one variant
  per concept per ad. The load sheet's brand rows are reference text only.
- Anchors mandatory: zero brand+product anchors in a carrier ad = build failure.
- Home-naming band: >=30% of pool headlines must contain "home" (floor, hard);
  soft ceiling 6 static-Home lines (warn + trim low-value excess). Home lines
  must span >=3 angle families.
- Bare brand lines allowed only when the static pool passes the band and the
  anchor is present; hard cap 4 per ad. Bare lines are a designed feature that
  pairs with the backbone's fused reinforcers at serve time.
- DKI ({KeyWord:Home Insurance}) counts toward the band only in ad groups
  whose keywords are home-dominant (>=50% contain "home", from paid_search.db);
  in carrier-term-dominant ad groups it behaves as a bare line; State Farm gets
  no DKI per CARRIER_FLAGS (the operator brand registry).
- Phrase-level dedupe: at most one headline per pool per distinctive phrase
  ("side by side", "in 3 minutes", ...) so the serving algorithm cannot pair
  two same-phrase lines into a redundant ad.
- Pools may ship at 13-14 headlines: 13 strong beat 15 padded.

EXIT CODES
----------
  0 = success (CSV written)
  1 = validation errors found (CSV NOT written; see report)
  2 = fatal error (DB missing, workbook missing, etc.)

USAGE
-----
    python bulk_import_ads_google.py
    python bulk_import_ads_google.py --workbook PATH --existing-rsas PATH
    python bulk_import_ads_google.py --dry-run     # validate without writing
"""

import argparse
import csv
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# Defensive UTF-8 reconfigure for Windows piping
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# =====================================================================
# CONSTANTS
# =====================================================================

DEFAULT_DB_PATH       = r"C:\Projects\google-ads-ops\db\paid_search.db"
DEFAULT_WORKBOOK      = r"C:\Projects\google-ads-ops\ad_assets\exports\input\rsa-load-ready-copy_v11.xlsx" # v11 REQUIRED: Carrier Variant Ladders sheet + fused UB lines
DEFAULT_RSA_EXPORT    = r"C:\Projects\google-ads-ops\ad_assets\exports\input\Ad_report_google_2026-06-03.csv"
DEFAULT_OUTPUT_DIR    = r"C:\Projects\google-ads-ops\ad_assets\exports\output"
DEFAULT_ACCOUNT_ID    = 1

HEADLINE_LIMIT    = 30
DESCRIPTION_LIMIT = 90

SHEET_UB          = "Universal Backbone"
SHEET_INTENT_SETS = "RSA Ad-Group Load Asset"
SHEET_SWAPS       = "Headline Swaps"

# Fields that must be consistent across existing RSAs within an ad-group instance.
# New RSA inherits these values; conflict between existing RSAs is a hard error.
# These affect FUNCTIONALITY (landing page, tracking, analytics).
HIGH_STAKES_INHERITED_FIELDS = [
    "Final URL",
    "Mobile final URL",
    "Tracking template",
    "Final URL suffix",
    "Custom parameter",
]

# Path 1 and Path 2 are display-only and DESIGN DECISIONS, not inherited state.
# They are sourced from LOGICAL_AD_GROUP_DESIGN[name]["paths"] = (Path1, Path2).
# If the design dict does not specify paths, fall back to inheritance with the
# same hard-fail-on-conflict logic (operator should populate paths explicitly).
PATH_FIELDS = ["Path 1", "Path 2"]

# Combined list (used by row builder to populate new RSA rows).
ALL_INHERITED_FIELDS = HIGH_STAKES_INHERITED_FIELDS + PATH_FIELDS

# =====================================================================
# v11 COPY-ASSEMBLY RULES (Carrier Variant Ladders + Build Script Rules)
# =====================================================================

SHEET_LADDERS = "Carrier Variant Ladders"

# Brand registry (EXTERNAL INPUT -- operator-maintained compliance flags).
# The ladders define WHAT to say; this registry defines WHO you may say it
# about and per-carrier constraints. Never hardcode brand lists elsewhere.
CARRIER_FLAGS: Dict[str, Dict[str, Any]] = {
    "State Farm": {"no_dki": True},
}

# Home-naming band (Build Script Rules):
#   - floor: >=30% of pool headlines must contain "home" (literal substring,
#     case-insensitive; "Homeowners" counts; Location line counts via its
#     static " Home Insurance" text; DKI counts conditionally via kw profile)
#   - soft ceiling: 6 static-Home lines (warn + trim low-value excess, not fail)
BAND_FLOOR_PCT    = 0.30
BAND_SOFT_CEILING = 6
BARE_CAP            = 4    # max bare brand-token lines per ad (hard)
TARGET_POOL_SIZE    = 15   # fill every RSA to 15 headlines (warn if inventory exhausts)
MIN_BRANDFREE_HOME  = 2    # static Home lines WITHOUT the brand token: designed
                           # pairing partners so bare lines assemble against
                           # brand-free Home context, not another brand line

# Brand-free Home candidates, preference order. ub:11 (Home Insurance Side by
# Side) is skipped when a bare side-by-side line is in/pending the pool --
# that pairing was flagged as redundant (phrase dedupe rule).
BRANDFREE_CANDIDATES = ["ub:14", "ub:12", "ub:13", "ub:11"]

# Fill-to-15 inventory (after dedupe + bare phases), tried in order; non-Home
# candidates are admitted first, Home candidates may drift past the soft
# ceiling with a warning. Text-level duplicates and phrase collisions skipped.
FILL_POOL_DEFAULT = [
    "swaps:Compare Online, No Pressure",
    "swaps:Highly Rated by Homeowners",
    "ub:2", "ub:8", "ub:7", "ub:6", "ub:9", "ub:5", "ub:4", "ub:3", "ub:1",
]

# Keyword-profile thresholds (DKI band counting / bare-equivalence)
KW_HOME_DOMINANT_FRAC    = 0.50   # >=50% of ad-group keywords contain "home"
KW_CARRIER_DOMINANT_FRAC = 0.50   # >=50% contain the carrier name (and not home-dominant)

# Phrase-level dedupe: at most ONE headline per pool may contain each
# distinctive phrase group (all substrings in a tuple must appear, after
# normalization). Serving algorithms pair headlines, so two "Side by Side"
# lines in one pool can assemble into a redundant served ad.
PHRASE_GROUPS: List[Tuple[str, ...]] = [
    ("side by side",),
    ("in 3 minutes",),
    ("no obligation",),
    ("top rated",),
    ("free", "quotes"),
]

# Ladder concept metadata: concept -> (angle_family, keep_rank)
# keep_rank: 0 keeps hardest (anchor) ... 6 trims first. Mirrors the
# Build Script Rules "order of keeps" for the 15-headline trim.
CONCEPT_META: Dict[str, Tuple[str, int]] = {
    "ANCHOR: brand+product":            ("anchor",     0),
    "Compare rates":                    ("cost",       1),
    "Beat the quote":                   ("cost",       1),
    "Renewal pain":                     ("renewal",    1),
    "Existing customer":                ("comparison", 4),
    "Price position":                   ("cost",       4),
    "Quality position":                 ("proof",      4),
    "Fit question":                     ("evaluation", 4),
    "Reviews (Carrier Reviews set)":    ("proof",      4),
    "Ratings & rates (Reviews set)":    ("proof",      4),
    "Coverage (Does-Carrier-Have set)": ("coverage",   4),
    "Insures-homes question":           ("coverage",   4),
    "Membership compare ([member])":    ("cost",       1),
    "Side by side (bare-only)":         ("comparison", 4),
    "How-compare (bare-only)":          ("evaluation", 4),
}

# Universal Backbone metadata: ub index -> (angle_family, keep_rank)
# ub:1 differentiator + ub:4 trust + ub:10 DKI are rank 2 per the trim rules;
# fused reinforcers (ub:11-14) are rank 3 (band capacity); Location/extra Home
# lines are rank 6 (first out; ceiling-trim candidates).
UB_META: Dict[int, Tuple[str, int]] = {
    1:  ("comparison", 2),   # We Make Insurers Compete (differentiator)
    2:  ("comparison", 6),   # Compare 2026 Home Rates
    3:  ("speed",      5),   # Get a Quote in 3 Minutes
    4:  ("trust",      2),   # Free, No-Obligation Quotes
    5:  ("proof",      5),   # Compare Top-Rated Insurers
    6:  ("proof",      5),   # Only A-Rated Carriers
    7:  ("geo",        6),   # {LOCATION(State)} Home Insurance
    8:  ("cost",       6),   # Lower Your Home Premium
    9:  ("comparison", 5),   # Compare Carriers Side by Side
    10: ("dki",        2),   # {KeyWord:Home Insurance}
    11: ("comparison", 3),   # Home Insurance Side by Side (fused)
    12: ("speed",      3),   # Home Insurance in 3 Minutes (fused)
    13: ("trust",      3),   # Free Home Insurance Quotes (fused)
    14: ("proof",      3),   # Top Rated Home Insurance (fused)
}

# Headline Swaps family lookup (legacy tiers)
SWAP_FAMILY: Dict[str, str] = {
    "Home Rates for Homeowners 50+":  "segment",
    "Mature Homeowner? Compare Now":  "segment",
    "Senior Homeowner Savings":       "segment",
    "Free, No-Obligation Quotes":     "trust",
    "Compare Online, No Pressure":    "trust",
    "Compare 50+ Top Carriers":       "proof",
    "Highly Rated by Homeowners":     "proof",
    "Get Covered in 3 Minutes":       "speed",
}

# =====================================================================
# LOCKED DESIGN
# =====================================================================

LOGICAL_AD_GROUP_DESIGN: Dict[str, Dict[str, Any]] = {
    # Carrier Conquesting -- full (7 CQ HLs)
    "Home-NonGeneric-Geico-Core":                  {"tier": "cq_full",       "carrier": "Geico",        "paths": ("Free-Quotes",    "Compare")},
    "Home-NonGeneric-Farmers Insurance-Core":      {"tier": "cq_full",       "carrier": "Farmers",      "paths": ("Compare-Quotes", "Free-Quotes")},
    "Home-NonGeneric-Hartford Insurance-Core":     {"tier": "cq_full",       "carrier": "Hartford",     "paths": ("Compare-Quotes", "Free-Quotes")},
    "Home-NonGeneric-Travelers Insurance-Core":    {"tier": "cq_full",       "carrier": "Travelers",    "paths": ("Compare-Quotes", "Free-Quotes")},
    "Home-Non-Generic-Home_Group2":                {"tier": "cq_full",       "carrier": "AmFam",        "paths": ("Compare-Quotes", "Free-Quotes")},
    "Home-Non-Generic-Home_Group3":                {"tier": "cq_full",       "carrier": "Root",         "paths": ("Compare-Quotes", "Free-Quotes")},

    # Carrier Conquesting -- short (5 CQ HLs)
    "Home-NonGeneric-State Farm Insurance-Core":   {"tier": "cq_short",      "carrier": "State Farm",   "paths": ("Compare-Quotes", "Free-Quotes")},
    "Home-Non-Generic-Home_Group5":                {"tier": "cq_short",      "carrier": "Allied Trust", "paths": ("Compare-Quotes", "Free-Quotes")},
    "Home-Non-Generic-Home_Group6":                {"tier": "cq_short",      "carrier": "21st Century", "paths": ("Compare-Quotes", "Free-Quotes")},

    # Other intent sets
    "Home-Non-Generic-Home_New":                   {"tier": "aggregator",    "carrier": None,           "paths": ("Compare-More",   "Free-Quotes")},
    "Home-Non-Generic-Home_CaRegion":              {"tier": "ca_non_renewal","carrier": None,           "paths": ("CA-Coverage",    "Find-Carriers")},

    # Backbone + senior/cost (no intent set)
    "Home-Generic-Fire-Core":                      {"tier": "backbone_plus", "carrier": None,           "paths": ("Fire-Insurance", "Free-Quotes")},
    "Home-Generic-Flood-Core":                     {"tier": "backbone_plus", "carrier": None,           "paths": ("Flood-Insurance","Free-Quotes")},
    "Home-Generic-Home Insurance-Core":            {"tier": "backbone_plus", "carrier": None,           "paths": ("Home-Insurance", "Compare-Quotes")},
    "Home-Non-Generic-Home_Group4":                {"tier": "backbone_plus", "carrier": None,           "paths": ("Home-Insurance", "Compare-Quotes")},
    "Home-Non-Generic-Home_FlRegion":              {"tier": "backbone_plus", "carrier": None,           "paths": ("Home-Insurance", "Compare-Quotes")},
    "Home-Non-Generic-Home_TxRegion":              {"tier": "backbone_plus", "carrier": None,           "paths": ("Home-Insurance", "Compare-Quotes")},

    # Excluded from this bulk import (handled separately)
    "Home-Non-Generic-Home_Group1":                {"tier": "excluded",      "carrier": None,           "paths": None},
}


# Per-tier rules. Asset references:
#   "ub:N"        -- Universal Backbone sheet, headline/description #N (1-indexed)
#   "is:N"        -- Intent set, headline/description #N within the intent set (1-indexed)
#   "is:all"      -- All headlines/descriptions from the intent set
#   "swaps:<text>"-- Headline Swaps ADD section, by exact text match
TIER_RULES: Dict[str, Optional[Dict[str, Any]]] = {
    # ------------------------------------------------------------------
    # CARRIER TIERS (v11 ladder engine).
    # Brand-token lines resolve through Carrier Variant Ladders:
    #   Home P1 -> Home P2 -> bare rung -> drop  (one variant per concept)
    # cq_full and cq_short are now IDENTICAL: the ladder's length math does
    # the per-carrier differentiation the old is:N skip-lists hard-coded.
    # Both tier names are kept so the design dict remains stable.
    # ------------------------------------------------------------------
    "cq_full": {
        "intent_set":       "Carrier Conquesting (template)",
        "ladder_concepts": [
            "ANCHOR: brand+product",        # mandatory -- zero anchors = build failure
            "Compare rates",
            "Beat the quote",
            "Existing customer",
            "Renewal pain",
            "Price position",
            "Quality position",
            "Fit question",
            "Side by side (bare-only)",
            "How-compare (bare-only)",
        ],
        "intent_set_descs": "is:all",        # 2 CQ descs ([Carrier]-substituted, 90-char hard gate)
        # Always-include backbone (DKI/Location handled conditionally downstream)
        "backbone_core":  ["ub:1", "ub:4", "ub:5", "ub:6", "ub:3", "ub:9", "ub:7", "ub:10"],
        # Home-naming band capacity, added only as needed (fused reinforcers first)
        "band_flex":      ["ub:14", "ub:12", "ub:13", "ub:11", "ub:2", "ub:8"],
        "extra_static":   [],
        "backbone_descs": ["ub:1", "ub:2"],
    },
    "cq_short": {
        "intent_set":       "Carrier Conquesting (template)",
        "ladder_concepts": [
            "ANCHOR: brand+product",
            "Compare rates",
            "Beat the quote",
            "Existing customer",
            "Renewal pain",
            "Price position",
            "Quality position",
            "Fit question",
            "Side by side (bare-only)",
            "How-compare (bare-only)",
        ],
        "intent_set_descs": "is:all",
        "backbone_core":  ["ub:1", "ub:4", "ub:5", "ub:6", "ub:3", "ub:9", "ub:7", "ub:10"],
        "band_flex":      ["ub:14", "ub:12", "ub:13", "ub:11", "ub:2", "ub:8"],
        "extra_static":   [],
        "backbone_descs": ["ub:1", "ub:2"],
    },
    "aggregator": {
        "intent_set":       "Aggregator-Competitor",
        "intent_set_hls":   "is:all",            # 6 Aggregator HLs (slots 1-6)
        "intent_set_descs": "is:all",
        # Backbone (slots 7-15): Location first (no DKI -- aggregator keywords like
        # "nerdwallet home insurance" would render awkwardly inserted)
        "backbone_hls": [
            "ub:7",                              # Location
            "ub:1",                              # Compare
            "swaps:Compare Online, No Pressure", # Trust/differentiator
            "ub:5",                              # Social proof
            "swaps:Highly Rated by Homeowners",  # Social proof
            "ub:4",                              # Trust
            "ub:3",                              # Speed
            "ub:8",                              # Cost (Lower Your Home Premium)
            "swaps:Senior Homeowner Savings",    # Cost (senior)
        ],
        "backbone_descs": ["ub:1", "ub:2"],
    },
    "ca_non_renewal": {
        "intent_set":       "California Non-Renewal & Availability",
        "intent_set_hls":   "is:all",            # 8 CA HLs (slots 1-8)
        "intent_set_descs": "is:all",            # 3 CA descs
        # Backbone (slots 9-15): DKI first (no Location -- CA campaign already geo-locked)
        "backbone_hls": [
            "ub:10",                             # DKI
            "ub:1",                              # Compare
            "ub:5",                              # Social proof
            "swaps:Highly Rated by Homeowners",  # Social proof
            "ub:4",                              # Trust
            "swaps:Get Covered in 3 Minutes",    # Speed
            "ub:3",                              # Speed
        ],
        "backbone_descs": ["ub:1"],
    },
    "backbone_plus": {
        "intent_set":       None,
        "intent_set_hls":   None,
        "intent_set_descs": None,
        # No intent set -- DKI + Location ARE the keyword-adaptive layer (slots 1-2),
        # then comparison mechanics, then trust/social/speed, then cost/senior emphasis.
        "backbone_hls": [
            "ub:10",                             # DKI -- most keyword-adaptive
            "ub:7",                              # Location -- geo-adaptive
            "ub:1",                              # Compare
            "ub:9",                              # Compare
            "ub:5",                              # Social proof
            "swaps:Highly Rated by Homeowners",  # Social proof
            "ub:4",                              # Trust
            "swaps:Compare Online, No Pressure", # Trust
            "ub:3",                              # Speed
            "swaps:Get Covered in 3 Minutes",    # Speed
            "ub:8",                              # Cost (Lower Your Home Premium)
            "swaps:Home Rates for Homeowners 50+",  # Cost (senior)
            "swaps:Senior Homeowner Savings",    # Cost (senior)
            "swaps:Mature Homeowner? Compare Now",  # Cost (senior)
            "swaps:Compare 50+ Top Carriers",    # Cost (senior)
        ],
        "backbone_descs": ["ub:1", "ub:2", "ub:3", "ub:4"],
    },
    "excluded": None,
}

# California campaign exception: mix CA distress headlines into all tiers.
# Priority order: intent headlines → comparison → CA distress → Trust (H14) → Speed (H15).
CA_TIER_VARIANTS: Dict[str, Dict[str, Any]] = {
    "backbone_plus_ca": {
        "intent_set":       None,
        "intent_set_hls":   None,
        "intent_set_descs": None,
        # DKI + Location (keyword-adaptive), then comparison, then CA distress, then Trust/Speed anchor.
        "backbone_hls": [
            "ub:10",                                 # DKI (H1)
            "ub:7",                                  # Location (H2)
            "ub:1",                                  # Compare (H3)
            "ub:9",                                  # Compare (H4)
            "ub:5",                                  # Compare/Social (H5)
            "lit:Non-Renewed in California?",      # CA Distress (H6)
            "lit:Dropped by Your Insurer?",        # CA Distress (H7)
            "lit:CA Coverage Still Available",      # CA Distress (H8)
            "lit:Insurer Left CA? Compare",        # CA Distress (H9)
            "lit:Find CA Carriers Still Writing",  # CA Distress (H10)
            "lit:Non-Renewal? We Can Help",        # CA Distress (H11)
            "lit:CA Home Insurance Options",       # CA Distress (H12)
            "lit:CA Rate Hike? See Options",       # CA Distress (H13)
            "ub:4",                                  # Trust (H14)
            "ub:3",                                  # Speed (H15)
        ],
        "backbone_descs": ["ub:1", "ub:2", "ub:3", "ub:4"],
    },
    "cq_full_ca": {
        "intent_set":       "Carrier Conquesting (template)",
        "ladder_concepts": [
            "ANCHOR: brand+product",
            "Compare rates",
            "Beat the quote",
            "Existing customer",
            "Renewal pain",
            "Price position",
            "Quality position",
            "Fit question",
            "Side by side (bare-only)",
            "How-compare (bare-only)",
        ],
        "intent_set_descs": "is:all",
        "backbone_core":  ["ub:1", "ub:4", "ub:3", "ub:10"],
        "band_flex":      ["ub:14", "ub:12", "ub:13", "ub:11", "ub:2", "ub:8"],
        # CA distress layer (campaign reason-for-being -- keep rank 3)
        "extra_static": [
            "lit:Non-Renewed in California?",
            "lit:CA Coverage Still Available",
            "lit:Dropped by Your Insurer?",
        ],
        "extra_static_rank": 3,
        "backbone_descs": ["ub:1", "ub:2"],
    },
    "cq_short_ca": {
        "intent_set":       "Carrier Conquesting (template)",
        "ladder_concepts": [
            "ANCHOR: brand+product",
            "Compare rates",
            "Beat the quote",
            "Existing customer",
            "Renewal pain",
            "Price position",
            "Quality position",
            "Fit question",
            "Side by side (bare-only)",
            "How-compare (bare-only)",
        ],
        "intent_set_descs": "is:all",
        "backbone_core":  ["ub:1", "ub:4", "ub:3", "ub:10"],
        "band_flex":      ["ub:14", "ub:12", "ub:13", "ub:11", "ub:2", "ub:8"],
        "extra_static": [
            "lit:Non-Renewed in California?",
            "lit:CA Coverage Still Available",
            "lit:Dropped by Your Insurer?",
            "lit:Insurer Left CA? Compare",
            "lit:Non-Renewal? We Can Help",
        ],
        "extra_static_rank": 3,
        "backbone_descs": ["ub:1", "ub:2"],
    },
    # ca_non_renewal stays as-is (CaRegion already has the right mix)
}

# Campaign-level tier overrides: maps campaign name → {base_tier: ca_variant_tier}
CAMPAIGN_TIER_OVERRIDES: Dict[str, Dict[str, str]] = {
    "HOME INSURANCE-CALIFORNIA-NB-SEARCH-ALL-USA-STE": {
        "backbone_plus": "backbone_plus_ca",
        "cq_full":       "cq_full_ca",
        "cq_short":      "cq_short_ca",
    },
}


# =====================================================================
# HELPERS
# =====================================================================

def normalize_location_insertion(text: str) -> Tuple[str, bool]:
    """
    Normalize location insertion to {Location(X):Your State} canonical form.

    Patterns handled:
      {LOCATION(X)}               -> {Location(X):Your State}
      {Location(X)}               -> {Location(X):Your State}
      {LOCATION(X):existing_fall} -> {Location(X):existing_fall}
    """
    original = text
    # No-fallback variant: add "Your State" default
    text = re.sub(
        r"\{LOCATION\(([^)]+)\)\}",
        lambda m: f"{{Location({m.group(1)}):Your State}}",
        text,
        flags=re.IGNORECASE,
    )
    # With-fallback variant: just normalize case of LOCATION -> Location
    text = re.sub(
        r"\{LOCATION\(([^)]+)\):([^}]*)\}",
        lambda m: f"{{Location({m.group(1)}):{m.group(2)}}}",
        text,
        flags=re.IGNORECASE,
    )
    return text, (text != original)


def substitute_carrier(text: str, carrier: Optional[str]) -> str:
    """Replace [Carrier] with carrier name; no-op if carrier is None."""
    if carrier is None:
        return text
    return text.replace("[Carrier]", carrier)


def visible_char_length(text: str) -> int:
    """
    Length of text counting dynamic insertion fallbacks (not the {...} tokens themselves).

    {KeyWord:fallback}            -> len(fallback)
    {Location(X):fallback}        -> len(fallback)
    """
    stripped = text
    stripped = re.sub(r"\{KeyWord:([^}]*)\}", lambda m: m.group(1), stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\{Location\([^)]+\):([^}]*)\}", lambda m: m.group(1), stripped, flags=re.IGNORECASE)
    return len(stripped)


def get_account_slug(conn: sqlite3.Connection, account_id: int) -> str:
    """Look up accounts.account_name and slugify."""
    row = conn.execute("SELECT account_name FROM accounts WHERE id = ?", [account_id]).fetchone()
    if not row or not row[0]:
        return f"account_{account_id}"
    slug = re.sub(r"[^a-z0-9]+", "_", row[0].lower()).strip("_")
    return slug or f"account_{account_id}"


def get_output_columns() -> List[str]:
    """Generate ordered list of output CSV columns matching Google Ads Editor format."""
    cols = [
        "Ad status",
        "Campaign",
        "Campaign ID",
        "Ad group",
        "Ad group ID",
        "Ad ID",
        "Ad type",
        "Final URL",
        "Mobile final URL",
        "Tracking template",
        "Final URL suffix",
        "Custom parameter",
        "Path 1",
        "Path 2",
    ]
    for i in range(1, 16):
        cols.append(f"Headline {i}")
        cols.append(f"Headline {i} position")
    for i in range(1, 5):
        cols.append(f"Description {i}")
        cols.append(f"Description {i} position")
    return cols


# =====================================================================
# WORKBOOK LOADER
# =====================================================================

def load_workbook_copy(workbook_path: Path) -> Dict[str, Any]:
    """
    Load all copy from the workbook into a structured dict.

    Returns:
        {
          "ub_headlines":   {1: "...", 2: "...", ..., 10: "..."},
          "ub_descriptions":{1: "...", 2: "...", ..., 4: "..."},
          "intent_sets": {
              "<intent set name>": {
                  "headlines":    {1: "...", 2: "...", ...},
                  "descriptions": {1: "...", ...},
              },
              ...
          },
          "swaps_add": {<exact_text>: <exact_text>, ...},  # for membership lookup
        }
    """
    if not workbook_path.exists():
        raise FileNotFoundError(f"Workbook not found: {workbook_path}")

    # Universal Backbone -- detect header row dynamically (scan for "Asset Type" in col 0)
    ub_raw = pd.read_excel(workbook_path, sheet_name=SHEET_UB, header=None)
    header_idx = None
    for i in range(len(ub_raw)):
        cell = ub_raw.iloc[i, 0]
        if pd.notna(cell) and str(cell).strip() == "Asset Type":
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(
            f"Could not find header row (with 'Asset Type' in column A) "
            f"in '{SHEET_UB}' sheet of {workbook_path.name}"
        )

    ub_df = pd.read_excel(workbook_path, sheet_name=SHEET_UB, header=header_idx)
    ub_df = ub_df.dropna(subset=["Asset Type", "Asset Text"])

    ub_headlines: Dict[int, str] = {}
    ub_descriptions: Dict[int, str] = {}
    hl_idx = 1
    desc_idx = 1
    for _, row in ub_df.iterrows():
        atype = str(row["Asset Type"]).strip()
        atext = str(row["Asset Text"]).strip()
        if atype == "Headline":
            ub_headlines[hl_idx] = atext
            hl_idx += 1
        elif atype == "Description":
            ub_descriptions[desc_idx] = atext
            desc_idx += 1

    # Intent sets from RSA Ad-Group Load Asset
    is_df = pd.read_excel(workbook_path, sheet_name=SHEET_INTENT_SETS)
    intent_sets: Dict[str, Dict[str, Dict[int, str]]] = defaultdict(
        lambda: {"headlines": {}, "descriptions": {}}
    )
    for intent_name, group in is_df.groupby("Intent Set"):
        hl_n = 1
        d_n = 1
        for _, row in group.iterrows():
            atype = str(row.get("Asset Type", "")).strip()
            atext = str(row.get("Asset Text", "")).strip()
            if not atext or atext == "nan":
                continue
            if atype == "Headline":
                intent_sets[intent_name]["headlines"][hl_n] = atext
                hl_n += 1
            elif atype == "Description":
                intent_sets[intent_name]["descriptions"][d_n] = atext
                d_n += 1

    # Headline Swaps -- only the ADD section (skip RETIRE)
    swaps_df = pd.read_excel(workbook_path, sheet_name=SHEET_SWAPS, header=None)
    swaps_add: Dict[str, str] = {}
    in_add_section = False
    header_row_seen = False
    for _, row in swaps_df.iterrows():
        c0 = str(row[0]).strip() if pd.notna(row[0]) else ""
        if "ADD" in c0.upper() and "new headlines" in c0.lower():
            in_add_section = True
            header_row_seen = False
            continue
        if "RETIRE" in c0.upper():
            in_add_section = False
            continue
        if in_add_section:
            if not header_row_seen:
                # next non-empty row in ADD section should be column headers (Headline, Angle, ...)
                if c0.lower() == "headline":
                    header_row_seen = True
                continue
            # Data row: column 0 is headline text.
            # Skip footnote / commentary rows (start with "Note:" or exceed reasonable HL length).
            if not c0 or c0 == "nan":
                continue
            if c0.lower().startswith("note:") or c0.startswith("*"):
                continue
            if len(c0) > 40:
                # Headlines are capped at 30 chars; a much-longer row is a comment/footnote
                continue
            swaps_add[c0] = c0

    # Carrier Variant Ladders (v11) -- length-tiered brand-token headline library.
    # Parsed into {concept: [rung, ...]} sorted by priority. A rung is "bare" when
    # its template lacks the "home" substring (brand line without the vertical).
    ladders: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    try:
        lad_raw = pd.read_excel(workbook_path, sheet_name=SHEET_LADDERS, header=None)
    except ValueError:
        lad_raw = None  # sheet absent (pre-v11 workbook) -- ladder tiers will hard-fail
    if lad_raw is not None:
        in_data = False
        for _, row in lad_raw.iterrows():
            c0 = str(row[0]).strip() if pd.notna(row[0]) else ""
            if c0 == "Concept":
                in_data = True
                continue
            if not in_data or not c0:
                continue
            template = str(row[2]).strip() if pd.notna(row[2]) else ""
            if not template:
                continue
            try:
                priority = int(float(row[1]))
            except (TypeError, ValueError):
                continue
            token = "[member]" if "[member]" in template else "[Carrier]"
            ladders[c0].append({
                "priority": priority,
                "template": template,
                "token":    token,
                "is_bare":  "home" not in template.lower(),
            })
        for concept in ladders:
            ladders[concept].sort(key=lambda r: r["priority"])

    return {
        "ub_headlines":    ub_headlines,
        "ub_descriptions": ub_descriptions,
        "intent_sets":     dict(intent_sets),
        "swaps_add":       swaps_add,
        "ladders":         dict(ladders),
    }


# =====================================================================
# REFERENCE RESOLVER (ub:N / is:N / swaps:TEXT)
# =====================================================================

def resolve_headline_ref(ref: str, copy: Dict[str, Any], intent_set_name: Optional[str]) -> str:
    """Resolve a single 'ub:N' / 'is:N' / 'swaps:TEXT' reference to actual text."""
    if ref.startswith("ub:"):
        n = int(ref.split(":", 1)[1])
        if n not in copy["ub_headlines"]:
            raise KeyError(f"UB headline #{n} not found in workbook")
        return copy["ub_headlines"][n]
    if ref.startswith("is:"):
        if intent_set_name is None:
            raise ValueError(f"Cannot resolve {ref} -- no intent_set in tier rules")
        if intent_set_name not in copy["intent_sets"]:
            raise KeyError(f"Intent set '{intent_set_name}' not found in workbook")
        n = int(ref.split(":", 1)[1])
        hls = copy["intent_sets"][intent_set_name]["headlines"]
        if n not in hls:
            raise KeyError(f"Intent set '{intent_set_name}' headline #{n} not found")
        return hls[n]
    if ref.startswith("swaps:"):
        text = ref.split(":", 1)[1]
        if text not in copy["swaps_add"]:
            raise KeyError(f"Headline Swaps ADD entry not found: '{text}'")
        return copy["swaps_add"][text]
    if ref.startswith("lit:"):
        return ref.split(":", 1)[1]
    raise ValueError(f"Unrecognized headline reference: {ref}")


def resolve_description_ref(ref: str, copy: Dict[str, Any], intent_set_name: Optional[str]) -> str:
    """Resolve a description reference. Only 'ub:N' and 'is:N' supported."""
    if ref.startswith("ub:"):
        n = int(ref.split(":", 1)[1])
        if n not in copy["ub_descriptions"]:
            raise KeyError(f"UB description #{n} not found in workbook")
        return copy["ub_descriptions"][n]
    if ref.startswith("is:"):
        if intent_set_name is None:
            raise ValueError(f"Cannot resolve {ref} -- no intent_set in tier rules")
        if intent_set_name not in copy["intent_sets"]:
            raise KeyError(f"Intent set '{intent_set_name}' not found in workbook")
        n = int(ref.split(":", 1)[1])
        descs = copy["intent_sets"][intent_set_name]["descriptions"]
        if n not in descs:
            raise KeyError(f"Intent set '{intent_set_name}' description #{n} not found")
        return descs[n]
    raise ValueError(f"Unrecognized description reference: {ref}")


def expand_intent_set_refs(spec: Any, intent_set_name: Optional[str], copy: Dict[str, Any], kind: str) -> List[str]:
    """
    Expand 'is:all' to a list of explicit 'is:N' refs.
    spec can be: None, "is:all", or a list like ["is:1", "is:2", ...].
    kind: "headlines" or "descriptions"
    """
    if spec is None:
        return []
    if spec == "is:all":
        if intent_set_name is None:
            return []
        items = copy["intent_sets"][intent_set_name][kind]
        return [f"is:{n}" for n in sorted(items.keys())]
    if isinstance(spec, list):
        return list(spec)
    raise ValueError(f"Unrecognized spec: {spec}")


# =====================================================================
# v11 LADDER RESOLUTION + KEYWORD PROFILE
# =====================================================================

def static_home_text(text: str) -> str:
    """
    Text used for Home-band counting: dynamic-insertion tokens removed.
    DKI displays the TRIGGERING KEYWORD (not the fallback), so its token
    contributes nothing statically; Location tokens display a location name,
    but any static text around them (e.g. ' Home Insurance') still counts.
    """
    t = re.sub(r"\{KeyWord:[^}]*\}", "", text, flags=re.IGNORECASE)
    t = re.sub(r"\{Location\([^)]+\):[^}]*\}", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\{LOCATION\([^)]+\)\}", "", t, flags=re.IGNORECASE)
    return t


def is_home_line(text: str) -> bool:
    """True if the static (post-token-strip) text contains 'home' (case-insensitive)."""
    return "home" in static_home_text(text).lower()


def _phrase_normalize(text: str) -> str:
    t = static_home_text(text).lower().replace("-", " ")
    return re.sub(r"\s+", " ", t).strip()


def phrase_groups_of(text: str) -> List[int]:
    """Indices of PHRASE_GROUPS matched by this text (all substrings present)."""
    norm = _phrase_normalize(text)
    hits = []
    for i, group in enumerate(PHRASE_GROUPS):
        if all(sub in norm for sub in group):
            hits.append(i)
    return hits


def _post_sub_length(template: str, token: str, name: str) -> int:
    """Post-substitution length: len(template) - len(token) + len(name)."""
    return len(template) - len(token) + len(name)


def resolve_home_rung(rungs: List[Dict[str, Any]], name: str) -> Tuple[Optional[str], Optional[str]]:
    """Highest-priority HOME rung fitting <=30 post-substitution. Returns (text, 'P<n>')."""
    for rung in rungs:
        if rung["is_bare"]:
            continue
        if _post_sub_length(rung["template"], rung["token"], name) <= HEADLINE_LIMIT:
            return rung["template"].replace(rung["token"], name), f"P{rung['priority']}"
    return None, None


def resolve_bare_rung(rungs: List[Dict[str, Any]], name: str) -> Tuple[Optional[str], Optional[str]]:
    """Highest-priority BARE rung fitting <=30 post-substitution. Returns (text, 'bare')."""
    for rung in rungs:
        if not rung["is_bare"]:
            continue
        if _post_sub_length(rung["template"], rung["token"], name) <= HEADLINE_LIMIT:
            return rung["template"].replace(rung["token"], name), "bare"
    return None, None


def get_keyword_profile(conn: Optional[sqlite3.Connection], ad_group_id: str,
                        carrier: Optional[str]) -> Dict[str, Any]:
    """
    Classify an ad group's active keywords to decide DKI behavior:
      home_dominant         -> DKI counts toward the Home band
      carrier_term_dominant -> DKI behaves as a BARE line (bare gate + cap apply)
      mixed                 -> DKI included; counts zero toward the band
      unknown               -> as mixed, flagged in report (schema/data unavailable)
    Defensive against schema drift: introspects keyword_instances columns.
    """
    profile = {"klass": "unknown", "n": 0, "home_frac": None, "carrier_frac": None}
    if conn is None:
        return profile
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(keyword_instances)").fetchall()]
        if not cols:
            return profile
        text_col = next((c for c in ("keyword_text", "keyword", "criterion_text",
                                     "normalized_text", "text") if c in cols), None)
        if text_col is None:
            return profile
        where = ["ad_group_id = ?"]
        params: List[Any] = [ad_group_id]
        if "status" in cols:
            where.append("UPPER(status) = 'ENABLED'")
        rows = conn.execute(
            f"SELECT {text_col} FROM keyword_instances WHERE {' AND '.join(where)}",
            params,
        ).fetchall()
    except sqlite3.Error:
        return profile

    kws = [str(r[0]).lower() for r in rows if r and r[0]]
    if not kws:
        return profile

    n = len(kws)
    home_frac = sum("home" in k for k in kws) / n
    carrier_frac = (sum(carrier.lower() in k for k in kws) / n) if carrier else 0.0
    profile.update({"n": n, "home_frac": round(home_frac, 3), "carrier_frac": round(carrier_frac, 3)})
    if home_frac >= KW_HOME_DOMINANT_FRAC:
        profile["klass"] = "home_dominant"
    elif carrier and carrier_frac >= KW_CARRIER_DOMINANT_FRAC:
        profile["klass"] = "carrier_term_dominant"
    else:
        profile["klass"] = "mixed"
    return profile


# =====================================================================
# COPY ASSEMBLY (v11 engine)
# =====================================================================
#
# Pipeline (carrier tiers):
#   1. Resolve ladder concepts at HOME rungs (P1 -> P2); track bare-pending
#   2. Static pool = ladder Home lines + extra_static + backbone_core
#   3. Band floor: add band_flex fused lines as needed (floor = 30% of 15)
#   4. Soft-ceiling trim: static Home > 6 -> remove low-value Home lines (warn)
#   5. Bare unlock (floor met + anchor present): bare-only concepts + bare
#      rungs of pending concepts, <= BARE_CAP; DKI consumes cap when the ad
#      group's keywords are carrier-term-dominant
#   6. Phrase dedupe (one line per distinctive phrase; band-aware winner)
#   7. Enrich thin pools (< MIN_RICH_POOL) with non-colliding flex lines
#   8. Trim to 15 by keep rank (floor-protected); pools may ship at 13-14
#   9. Final gates: anchor, floor at actual size, >=3 families among Home
#      lines, bare <= cap, lengths
#
# Legacy (non-carrier) tiers resolve their fixed lists, then run the same
# band / dedupe / trim post-pipeline (steps 3-9, no bare phase).
# =====================================================================

def _hl_entry(text: str, ref: str, family: str, rank: int,
              concept: Optional[str] = None, rung: Optional[str] = None,
              is_bare: bool = False, is_dki: bool = False) -> Dict[str, Any]:
    return {
        "text": text, "ref": ref, "family": family, "rank": rank,
        "concept": concept, "rung": rung,
        "is_home": (not is_dki) and is_home_line(text),
        "is_bare": is_bare, "is_dki": is_dki,
    }


def _static_home_count(pool: List[Dict[str, Any]]) -> int:
    return sum(1 for h in pool if h["is_home"])


def _band_floor(pool_size: int) -> int:
    import math
    return math.ceil(BAND_FLOOR_PCT * pool_size)


def _legacy_ref_meta(ref: str, text: str) -> Tuple[str, int]:
    """(family, keep_rank) for legacy-tier refs (is:N / lit: / swaps: / ub:N)."""
    if ref.startswith("ub:"):
        n = int(ref.split(":", 1)[1])
        return UB_META.get(n, ("other", 5))
    if ref.startswith("lit:"):
        return ("distress", 3)
    if ref.startswith("swaps:"):
        return (SWAP_FAMILY.get(text, "other"), 5)
    if ref.startswith("is:"):
        return ("intent", 3)
    return ("other", 5)


def _resolve_ub_entry(n: int, copy: Dict[str, Any], carrier: Optional[str],
                      warnings: List[str], added_for_band: bool = False) -> Optional[Dict[str, Any]]:
    """Resolve a ub:N headline into an HL entry (normalizing Location syntax)."""
    if n not in copy["ub_headlines"]:
        return None
    text = copy["ub_headlines"][n]
    text = substitute_carrier(text, carrier)
    text, changed = normalize_location_insertion(text)
    if changed:
        warnings.append(f"Location insertion normalized in headline: '{text}'")
    family, rank = UB_META.get(n, ("other", 5))
    if added_for_band:
        rank = 3  # band-needed flex keeps harder than discretionary backbone
    return _hl_entry(text, f"ub:{n}", family, rank, is_dki=(n == 10))


def _dedupe_phrases(pool: List[Dict[str, Any]], floor: int,
                    decisions: List[str]) -> List[Dict[str, Any]]:
    """
    Enforce one-headline-per-phrase-group. Winner selection, in order:
      1. A Home line whose removal would break the band floor wins
      2. Lowest keep rank wins
      3. Carrier-specific (ladder concept) beats fused/UB-generic
      4. Earlier assembly position wins (stable)
    """
    result = list(pool)
    for gi, group in enumerate(PHRASE_GROUPS):
        members = [h for h in result if gi in phrase_groups_of(h["text"])]
        if len(members) <= 1:
            continue
        home_count = _static_home_count(result)

        def keep_score(h: Dict[str, Any]) -> Tuple:
            # Lower tuple = stronger keep claim
            band_critical = h["is_home"] and (home_count - 1) < floor
            return (
                0 if band_critical else 1,
                h["rank"],
                0 if h["concept"] else 1,
                result.index(h),
            )

        members.sort(key=keep_score)
        winner, losers = members[0], members[1:]
        for loser in losers:
            result.remove(loser)
            decisions.append(
                f"phrase '{' + '.join(group)}': kept '{winner['text']}' "
                f"({winner['ref']}), dropped '{loser['text']}' ({loser['ref']})"
            )
    return result


def _trim_to_limit(pool: List[Dict[str, Any]], limit: int, floor: int,
                   notes: List[str]) -> List[Dict[str, Any]]:
    """
    Trim pool to <= limit by keep rank (highest rank out first; later assembly
    position breaks ties). Never removes the anchor; protects the band floor by
    skipping Home lines whose removal would break it.
    """
    result = list(pool)
    while len(result) > limit:
        home_count = _static_home_count(result)
        candidates = sorted(
            (h for h in result if h["rank"] > 0),
            key=lambda h: (-h["rank"], -result.index(h)),
        )
        removed = False
        for cand in candidates:
            if cand["is_home"] and (home_count - 1) < floor:
                continue
            result.remove(cand)
            notes.append(f"15-trim: removed '{cand['text']}' (rank {cand['rank']}, {cand['ref']})")
            removed = True
            break
        if not removed:
            break  # cannot trim further without breaking gates; length check will flag
    return result


def assemble_copy_for_tier(
    tier_name: str,
    carrier: Optional[str],
    copy: Dict[str, Any],
    kw_profile: Optional[Dict[str, Any]] = None,
) -> Tuple[List[str], List[str], List[str], List[str], Dict[str, Any]]:
    """
    Assemble headlines and descriptions for an RSA per the tier rules.

    Returns:
        (headlines, descriptions, warnings, errors, pool_report)

    pool_report carries the v11 review artifacts: rung outcomes per concept,
    band tally, angle families, bare counts, dedupe decisions, remediations.
    """
    warnings: List[str] = []
    errors: List[str] = []
    report: Dict[str, Any] = {
        "tier": tier_name, "carrier": carrier,
        "rungs": {},          # concept -> P1 / P2 / bare / dropped / bare_locked / trimmed
        "dedupe": [],         # phrase-dedupe decision strings
        "remediations": [],   # band fixes / trims / enrichment
        "static_home": 0, "home_pct": 0.0, "families": [],
        "bare_count": 0, "dki_status": "n/a", "n_headlines": 0,
    }

    rules = TIER_RULES.get(tier_name) or CA_TIER_VARIANTS.get(tier_name)
    if rules is None:
        errors.append(f"Tier '{tier_name}' is excluded or unknown")
        return [], [], warnings, errors, report

    intent_set_name = rules.get("intent_set")
    kw_profile = kw_profile or {"klass": "unknown"}
    flags = CARRIER_FLAGS.get(carrier or "", {})
    working_floor = _band_floor(15)
    pool: List[Dict[str, Any]] = []
    pending_bare: List[str] = []   # concepts awaiting the bare phase

    is_ladder_tier = bool(rules.get("ladder_concepts"))

    if is_ladder_tier:
        # ---- Phase 1: ladder Home rungs -------------------------------
        if not copy.get("ladders"):
            errors.append("Workbook has no 'Carrier Variant Ladders' sheet (required for carrier tiers)")
            return [], [], warnings, errors, report
        if not carrier:
            errors.append(f"Tier '{tier_name}' requires a carrier in the design dict")
            return [], [], warnings, errors, report

        for concept in rules["ladder_concepts"]:
            rungs = copy["ladders"].get(concept)
            if not rungs:
                errors.append(f"Ladder concept missing from workbook: '{concept}'")
                continue
            family, rank = CONCEPT_META.get(concept, ("other", 4))
            text, rung_label = resolve_home_rung(rungs, carrier)
            if text is not None:
                pool.append(_hl_entry(text, f"ladder:{concept}", family, rank,
                                      concept=concept, rung=rung_label))
                report["rungs"][concept] = rung_label
            else:
                bare_text, _ = resolve_bare_rung(rungs, carrier)
                if bare_text is not None:
                    pending_bare.append(concept)
                    report["rungs"][concept] = "pending_bare"
                else:
                    report["rungs"][concept] = "dropped"

        # Anchor gate (hard)
        anchor_present = any(h["concept"] == "ANCHOR: brand+product" for h in pool)
        if not anchor_present:
            errors.append(
                f"BUILD FAILURE: no brand+product anchor fits for carrier "
                f"'{carrier}' (name length {len(carrier)})"
            )

        # ---- Phase 2: extra static (CA distress / intent angle lines) ----
        extra_rank = int(rules.get("extra_static_rank", 3))
        for ref in rules.get("extra_static", []):
            try:
                text = resolve_headline_ref(ref, copy, intent_set_name)
            except (KeyError, ValueError) as e:
                errors.append(f"Resolve failure for extra_static {ref}: {e}")
                continue
            text = substitute_carrier(text, carrier)
            family = "distress" if ref.startswith("lit:") else "intent"
            pool.append(_hl_entry(text, ref, family, extra_rank))
        for ref in rules.get("backbone_core", []):
            n = int(ref.split(":", 1)[1])
            if n == 10:
                continue  # DKI handled below
            entry = _resolve_ub_entry(n, copy, carrier, warnings)
            if entry is None:
                errors.append(f"UB headline #{n} not found in workbook")
                continue
            pool.append(entry)

        # ---- DKI handling ---------------------------------------------
        dki_in_core = any(r == "ub:10" for r in rules.get("backbone_core", []))
        dki_entry = None
        if flags.get("no_dki"):
            report["dki_status"] = "excluded (registry: no DKI)"
        elif dki_in_core:
            dki_entry = _resolve_ub_entry(10, copy, carrier, warnings)
            klass = kw_profile.get("klass", "unknown")
            if klass == "home_dominant":
                report["dki_status"] = "counts toward band (home-dominant keywords)"
                pool.append(dki_entry)
            elif klass == "carrier_term_dominant":
                report["dki_status"] = "bare-equivalent (carrier-term keywords; bare gate applies)"
                # appended in the bare phase, against the cap
            else:
                report["dki_status"] = f"included, counts zero ({klass})"
                pool.append(dki_entry)
    else:
        # ---- Legacy (non-carrier) tiers --------------------------------
        hl_refs: List[str] = []
        hl_refs += expand_intent_set_refs(rules.get("intent_set_hls"), intent_set_name, copy, "headlines")
        hl_refs += list(rules.get("backbone_hls") or [])
        for ref in hl_refs:
            try:
                text = resolve_headline_ref(ref, copy, intent_set_name)
            except (KeyError, ValueError) as e:
                errors.append(f"Resolve failure for headline {ref}: {e}")
                continue
            text = substitute_carrier(text, carrier)
            text, changed = normalize_location_insertion(text)
            if changed:
                warnings.append(f"Location insertion normalized in headline: '{text}'")
            family, rank = _legacy_ref_meta(ref, text if not ref.startswith("swaps:")
                                            else ref.split(":", 1)[1])
            is_dki = ref == "ub:10"
            if is_dki and flags.get("no_dki"):
                report["dki_status"] = "excluded (registry: no DKI)"
                continue
            if is_dki:
                klass = kw_profile.get("klass", "unknown")
                report["dki_status"] = (
                    "counts toward band (home-dominant keywords)" if klass == "home_dominant"
                    else f"included, counts zero ({klass})"
                )
            pool.append(_hl_entry(text, ref, family, rank, is_dki=is_dki))

    # ---- Phase 3: band floor remediation (both paths) -----------------
    flex_refs = list(rules.get("band_flex") or ["ub:11", "ub:14", "ub:12", "ub:13"])
    flex_iter = iter(flex_refs)
    while _static_home_count(pool) < working_floor:
        ref = next(flex_iter, None)
        if ref is None:
            break
        n = int(ref.split(":", 1)[1])
        if any(h["ref"] == ref for h in pool):
            continue
        entry = _resolve_ub_entry(n, copy, carrier, warnings, added_for_band=True)
        if entry is None:
            continue
        pool.append(entry)
        report["remediations"].append(f"band floor: added '{entry['text']}' ({ref})")

    # ---- Phase 3b: brand-free Home guarantee (ladder tiers) -------------
    # Bare brand lines need brand-FREE Home pairing partners at serve time;
    # if every Home line carries the brand token, the serving algorithm can
    # only assemble brand x brand repetition. Fused reinforcers are the
    # phrase-partners of the generics they echo, so phrase dedupe swaps the
    # generic out -- diversity gained, zero redundancy added.
    if is_ladder_tier and carrier:
        sbs_pending = "Side by side (bare-only)" in (rules.get("ladder_concepts") or [])
        for ref in BRANDFREE_CANDIDATES:
            # Count only trim-safe (rank <= 3) brand-free Home lines: Location
            # (rank 6) is first out in the ceiling trim and cannot be relied on.
            brandfree = sum(1 for h in pool
                            if h["is_home"] and h.get("concept") is None and h["rank"] <= 3)
            if brandfree >= MIN_BRANDFREE_HOME:
                break
            if ref == "ub:11" and sbs_pending:
                continue  # would pair-collide with the bare side-by-side line
            if any(h["ref"] == ref for h in pool):
                continue
            entry = _resolve_ub_entry(int(ref.split(":", 1)[1]), copy, carrier,
                                      warnings, added_for_band=True)
            if entry is None:
                continue
            pool.append(entry)
            report["remediations"].append(
                f"brand-free Home: added '{entry['text']}' ({ref}) as bare-line pairing partner")

    # ---- Phase 4: soft-ceiling trim ------------------------------------
    trimmed_home_concepts: List[Dict[str, Any]] = []   # restorable in fill phase
    while _static_home_count(pool) > BAND_SOFT_CEILING:
        excess = sorted(
            (h for h in pool if h["is_home"] and h["rank"] >= 4),
            key=lambda h: (-h["rank"], -pool.index(h)),
        )
        if not excess:
            warnings.append(
                f"Static Home count {_static_home_count(pool)} exceeds soft ceiling "
                f"{BAND_SOFT_CEILING}; no low-value Home lines left to trim"
            )
            break
        victim = excess[0]
        pool.remove(victim)
        if victim.get("concept"):
            report["rungs"][victim["concept"]] = f"{victim['rung']} (ceiling-trimmed)"
            trimmed_home_concepts.append(victim)
            # Trimmed concept re-enters the bare queue if a bare rung fits:
            # its non-Home brand variant costs no Home-band budget.
            v_rungs = copy.get("ladders", {}).get(victim["concept"], [])
            if v_rungs and carrier and resolve_bare_rung(v_rungs, carrier)[0] is not None:
                if victim["concept"] not in pending_bare:
                    pending_bare.append(victim["concept"])
        report["remediations"].append(
            f"soft ceiling: removed Home line '{victim['text']}' ({victim['ref']})"
        )

    # ---- Phase 5: bare unlock (carrier tiers only) ----------------------
    if is_ladder_tier and carrier:
        anchor_present = any(h["concept"] == "ANCHOR: brand+product" for h in pool)
        floor_met = _static_home_count(pool) >= working_floor
        bare_budget = BARE_CAP
        if floor_met and anchor_present:
            # DKI-as-bare consumes cap first (it precedes concepts in serve value)
            if dki_entry is not None and kw_profile.get("klass") == "carrier_term_dominant":
                pool.append({**dki_entry, "is_bare": True})
                bare_budget -= 1
            for concept in pending_bare:
                if bare_budget <= 0:
                    report["rungs"][concept] = "bare_locked (cap reached)"
                    continue
                rungs = copy["ladders"].get(concept, [])
                family, rank = CONCEPT_META.get(concept, ("other", 4))
                text, _ = resolve_bare_rung(rungs, carrier)
                if text is None:
                    report["rungs"][concept] = "dropped"
                    continue
                pool.append(_hl_entry(text, f"ladder:{concept}", family, rank,
                                      concept=concept, rung="bare", is_bare=True))
                prev = str(report["rungs"].get(concept, ""))
                report["rungs"][concept] = (
                    f"bare (Home rung ceiling-trimmed)" if "ceiling-trimmed" in prev else "bare")
                bare_budget -= 1
        else:
            reason = "band floor unmet on static lines" if not floor_met else "anchor missing"
            for concept in pending_bare:
                report["rungs"][concept] = f"bare_locked ({reason})"
            if dki_entry is not None and kw_profile.get("klass") == "carrier_term_dominant":
                report["dki_status"] += f" -- excluded: bare locked ({reason})"

    # ---- Phase 6: phrase dedupe -----------------------------------------
    pool = _dedupe_phrases(pool, working_floor, report["dedupe"])

    # ---- Phase 7: fill to 15 ---------------------------------------------
    # Every RSA ships with TARGET_POOL_SIZE headlines unless copy inventory
    # truly exhausts after dedupe/gates (warned). Non-Home candidates are
    # admitted first; Home candidates may drift past the soft ceiling on the
    # second pass, each drift warned.
    if len(pool) < TARGET_POOL_SIZE:
        fill_refs: List[str] = []
        for ref in flex_refs + list(rules.get("fill_pool") or FILL_POOL_DEFAULT):
            if ref not in fill_refs:
                fill_refs.append(ref)
        pool_phrases = {g for h in pool for g in phrase_groups_of(h["text"])}
        for allow_home_drift in (False, True):
            if allow_home_drift:
                # Restore ceiling-trimmed conquest Home lines first: a designed
                # brand line beats a generic Home line when drifting anyway.
                # One-variant-per-concept holds: skip any concept already in the
                # pool (e.g., re-entered as its bare variant).
                for victim in trimmed_home_concepts:
                    if len(pool) >= TARGET_POOL_SIZE:
                        break
                    if any(h.get("concept") == victim["concept"] for h in pool):
                        continue
                    if any(h["text"] == victim["text"] for h in pool):
                        continue
                    if any(gi in pool_phrases for gi in phrase_groups_of(victim["text"])):
                        continue
                    warnings.append(
                        f"Soft-ceiling drift: Home count {_static_home_count(pool) + 1} > "
                        f"{BAND_SOFT_CEILING} after restore ('{victim['text']}')")
                    pool.append(victim)
                    pool_phrases |= set(phrase_groups_of(victim["text"]))
                    report["rungs"][victim["concept"]] = f"{victim['rung']} (restored in fill)"
                    report["remediations"].append(
                        f"fill-to-15: restored '{victim['text']}' (ladder:{victim['concept']})")
            for ref in fill_refs:
                if len(pool) >= TARGET_POOL_SIZE:
                    break
                if any(h["ref"] == ref for h in pool):
                    continue
                try:
                    if ref.startswith("ub:"):
                        entry = _resolve_ub_entry(int(ref.split(":", 1)[1]), copy,
                                                  carrier, warnings)
                    else:
                        text = resolve_headline_ref(ref, copy, intent_set_name)
                        text = substitute_carrier(text, carrier)
                        text, _ch = normalize_location_insertion(text)
                        family, rank = _legacy_ref_meta(
                            ref, ref.split(":", 1)[1] if ref.startswith("swaps:") else text)
                        entry = _hl_entry(text, ref, family, rank)
                except (KeyError, ValueError):
                    continue
                if entry is None or entry["is_dki"]:
                    continue
                if visible_char_length(entry["text"]) > HEADLINE_LIMIT:
                    continue
                if any(h["text"] == entry["text"] for h in pool):
                    continue  # text-level duplicate (e.g., a swap echoing a UB line)
                if entry["is_home"] and _static_home_count(pool) >= BAND_SOFT_CEILING:
                    if not allow_home_drift:
                        continue
                    warnings.append(
                        f"Soft-ceiling drift: Home count {_static_home_count(pool) + 1} > "
                        f"{BAND_SOFT_CEILING} after fill ('{entry['text']}')")
                if any(gi in pool_phrases for gi in phrase_groups_of(entry["text"])):
                    continue
                pool.append(entry)
                pool_phrases |= set(phrase_groups_of(entry["text"]))
                report["remediations"].append(f"fill-to-15: added '{entry['text']}' ({ref})")
        if len(pool) < TARGET_POOL_SIZE:
            warnings.append(
                f"Pool below {TARGET_POOL_SIZE} headlines ({len(pool)}): copy inventory "
                f"exhausted after dedupe and gates")

    # ---- Phase 8: trim to 15 ---------------------------------------------
    if len(pool) > 15:
        pool = _trim_to_limit(pool, 15, working_floor, report["remediations"])

    # ---- Phase 8b: deterministic slot order --------------------------------
    # All assets are Unpinned, so slot order never affects serving -- this is
    # the designed review sequence: anchor, conquest Home lines, bare brand
    # lines, intent/distress statics, brand-free Home, generics, DKI last.
    def _slot_class(h: Dict[str, Any]) -> int:
        if h["is_dki"]:
            return 6
        if h.get("concept") == "ANCHOR: brand+product":
            return 0
        if h.get("concept") and not h["is_bare"]:
            return 1
        if h["is_bare"]:
            return 2
        if h["ref"].startswith(("is:", "lit:")):
            return 3
        if h["is_home"]:
            return 4
        return 5
    _orig_pos = {id(h): i for i, h in enumerate(pool)}
    pool.sort(key=lambda h: (_slot_class(h), _orig_pos[id(h)]))

    # ---- Phase 9: final validation -----------------------------------------
    headlines: List[str] = []
    for h in pool:
        text = h["text"]
        n_chars = visible_char_length(text)
        if n_chars > HEADLINE_LIMIT:
            errors.append(f"Headline exceeds {HEADLINE_LIMIT} chars ({n_chars}): '{text}'")
        if "[Carrier]" in text or "[member]" in text:
            errors.append(f"Token substitution failed (placeholder remains): '{text}'")
        if "  " in text:
            warnings.append(f"Double space in headline: '{text}'")
        headlines.append(text)

    static_home = _static_home_count(pool)
    actual_floor = _band_floor(len(pool)) if pool else 0
    dki_band_bonus = 1 if (
        any(h["is_dki"] for h in pool) and kw_profile.get("klass") == "home_dominant"
    ) else 0
    effective_home = static_home + dki_band_bonus
    if pool and effective_home < actual_floor:
        errors.append(
            f"Home-naming band FAILURE: {effective_home} Home-counting lines "
            f"(static {static_home} + DKI {dki_band_bonus}) < floor {actual_floor} "
            f"({BAND_FLOOR_PCT:.0%} of {len(pool)})"
        )
    home_families = sorted({h["family"] for h in pool if h["is_home"]})
    if pool and static_home > 0 and len(home_families) < 3:
        errors.append(
            f"Home-line diversity FAILURE: families {home_families} (< 3 required)"
        )
    bare_count = sum(1 for h in pool if h["is_bare"])
    if bare_count > BARE_CAP:
        errors.append(f"Bare-line cap exceeded: {bare_count} > {BARE_CAP}")

    brandfree_home = sum(1 for h in pool if h["is_home"] and h.get("concept") is None)
    report.update({
        "static_home": static_home,
        "home_pct": round(static_home / len(pool), 3) if pool else 0.0,
        "families": home_families,
        "bare_count": bare_count,
        "brandfree_home": brandfree_home,
        "n_headlines": len(pool),
    })

    # ---- Descriptions ------------------------------------------------------
    d_refs: List[str] = []
    d_refs += expand_intent_set_refs(rules.get("intent_set_descs"), intent_set_name, copy, "descriptions")
    d_refs += list(rules.get("backbone_descs") or [])

    descriptions: List[str] = []
    for ref in d_refs:
        try:
            text = resolve_description_ref(ref, copy, intent_set_name)
        except (KeyError, ValueError) as e:
            errors.append(f"Resolve failure for description {ref}: {e}")
            continue
        text = substitute_carrier(text, carrier)
        text, changed = normalize_location_insertion(text)
        if changed:
            warnings.append(f"Location insertion normalized in description: '{text}'")
        n_chars = visible_char_length(text)
        if n_chars > DESCRIPTION_LIMIT:
            errors.append(f"Description exceeds {DESCRIPTION_LIMIT} chars ({n_chars}): '{text}'")
        if "[Carrier]" in text or "[member]" in text:
            errors.append(f"Token substitution failed in description: '{text}'")
        descriptions.append(text)

    # Headline count bounds
    if len(headlines) < 3:
        errors.append(f"Too few headlines ({len(headlines)}); minimum is 3")
    if len(headlines) > 15:
        warnings.append(f"Too many headlines ({len(headlines)}); truncating to 15")
        headlines = headlines[:15]

    # Description count bounds
    if len(descriptions) < 2:
        errors.append(f"Too few descriptions ({len(descriptions)}); minimum is 2")
    if len(descriptions) > 4:
        warnings.append(f"Too many descriptions ({len(descriptions)}); truncating to 4")
        descriptions = descriptions[:4]

    return headlines, descriptions, warnings, errors, report


# =====================================================================
# EXISTING RSA LOADER
# =====================================================================

def load_existing_rsas(rsa_export_path: Path) -> pd.DataFrame:
    """Load Ad_report CSV; filter to active RSAs only. Returns DataFrame with string IDs."""
    if not rsa_export_path.exists():
        raise FileNotFoundError(f"Existing RSA export not found: {rsa_export_path}")

    # Google Ads UI exports have 2 header lines (title + date range) before the actual headers
    df = pd.read_csv(rsa_export_path, skiprows=2, dtype=str, keep_default_na=False)

    # Filter to actual RSA rows (drop totals/summary rows)
    df = df[df["Ad type"].str.strip() == "Responsive search ad"].copy()
    df = df[df["Ad status"].str.strip() == "Enabled"].copy()

    # Ensure ID columns are clean strings (no float coercion artifacts).
    # NOTE: escape the dot in the regex (\.) -- unescaped "." matches any character,
    # which would silently corrupt IDs ending in "X0" (e.g., 23394808300 -> 233948083).
    for col in ["Campaign ID", "Ad group ID", "Ad ID"]:
        df[col] = df[col].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()

    # Normalize Google UI export placeholder "--" to empty string for tracking/URL fields.
    # The UI export uses "--" to mean "no value" but Editor reads it as a literal
    # (e.g., http://-- is a malformed tracking URL). Final URL is NOT normalized
    # because it's always a real URL in the export.
    for col in ["Mobile final URL", "Tracking template", "Final URL suffix",
                "Custom parameter", "Path 1", "Path 2"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip().replace("--", "")

    return df.reset_index(drop=True)


def identify_top_rsa(instance_rsas: pd.DataFrame) -> Optional[pd.Series]:
    """
    Identify the top-performing RSA in an ad group instance.

    Ranking (descending priority):
      1. Conversions (raw count -- direct measure of "drives more conversions")
      2. Conversions / Impressions (CVR per impression -- efficiency tiebreaker)
      3. Impressions (more data wins when conversions tied at 0)
      4. Ad ID ascending (oldest = lowest ID, stable tiebreaker)

    Returns the row of the top RSA, or None if input is empty.
    """
    if len(instance_rsas) == 0:
        return None
    if len(instance_rsas) == 1:
        return instance_rsas.iloc[0]

    df = instance_rsas.copy()
    for col, src in [("_impr", "Impr."), ("_conv", "Conversions")]:
        if src not in df.columns:
            df[col] = 0.0
        else:
            df[col] = pd.to_numeric(
                df[src].astype(str).str.replace(",", "").str.replace("--", "0").str.replace("<", ""),
                errors="coerce",
            ).fillna(0)
    df["_conv_per_impr"] = df["_conv"] / df["_impr"].replace(0, 1)

    df = df.sort_values(
        by=["_conv", "_conv_per_impr", "_impr", "Ad ID"],
        ascending=[False, False, False, True],
    )
    return df.iloc[0]


def validate_url_path_inheritance(
    instance_rsas: pd.DataFrame,
    campaign_name: str,
    ad_group_name: str,
    design_paths: Optional[Tuple[Optional[str], Optional[str]]],
) -> Tuple[Dict[str, str], List[str], List[str]]:
    """
    For an ad-group instance with one or more existing RSAs:
      - HIGH_STAKES fields (URL, tracking, etc.): hard-fail on conflict
      - PATH fields: read from design dict if provided; otherwise fall back to
        inheritance with hard-fail-on-conflict (architect-approved fallback)

    Returns:
        (inherited_fields_dict, warnings, errors)
    """
    warnings: List[str] = []
    errors: List[str] = []
    inherited: Dict[str, str] = {}

    if len(instance_rsas) == 0:
        errors.append(
            f"No existing RSAs found for {campaign_name} / {ad_group_name} -- "
            f"cannot inherit Final URL etc. Skipping."
        )
        return inherited, warnings, errors

    if len(instance_rsas) == 1:
        warnings.append(
            f"Only 1 existing RSA in {campaign_name} / {ad_group_name} (expected 2). "
            f"Proceeding with single-RSA inheritance."
        )

    # --- High-stakes fields: hard-fail on conflict ---
    for field in HIGH_STAKES_INHERITED_FIELDS:
        if field not in instance_rsas.columns:
            inherited[field] = ""
            continue
        values = instance_rsas[field].fillna("").astype(str).str.strip().tolist()
        unique_vals = list(set(values))
        if len(unique_vals) == 1:
            inherited[field] = unique_vals[0]
        else:
            errors.append(
                f"CONFLICT in {field} for {campaign_name} / {ad_group_name}: "
                f"{unique_vals!r}. Resolve in account before re-running."
            )
            inherited[field] = ""

    # --- Path fields: design dict first, inheritance fallback ---
    if design_paths is not None and design_paths != (None, None):
        # Design dict provides paths -- use them, ignore existing RSA values
        path1, path2 = design_paths
        inherited["Path 1"] = path1 or ""
        inherited["Path 2"] = path2 or ""
    else:
        # No design override -- fall back to inheritance with conflict detection
        for field in PATH_FIELDS:
            if field not in instance_rsas.columns:
                inherited[field] = ""
                continue
            values = instance_rsas[field].fillna("").astype(str).str.strip().tolist()
            unique_vals = list(set(values))
            if len(unique_vals) == 1:
                inherited[field] = unique_vals[0]
            else:
                errors.append(
                    f"CONFLICT in {field} for {campaign_name} / {ad_group_name}: "
                    f"{unique_vals!r}. No path specified in design dict; "
                    f"add a 'paths' entry to LOGICAL_AD_GROUP_DESIGN to resolve."
                )
                inherited[field] = ""

    return inherited, warnings, errors


# =====================================================================
# DATABASE QUERY (active ad-group instances)
# =====================================================================

INSTANCE_QUERY = """
SELECT
    c.campaign_id,
    c.campaign_name,
    ag.ad_group_id,
    ag.ad_group_name
FROM campaigns c
JOIN ad_groups ag ON ag.campaign_id = c.campaign_id
WHERE c.account_id = ?
  AND c.status     = 'ENABLED'
  AND ag.status    = 'ENABLED'
ORDER BY c.campaign_name, ag.ad_group_name
"""


def query_active_ad_group_instances(conn: sqlite3.Connection, account_id: int) -> pd.DataFrame:
    """Returns DataFrame with active campaign/ad-group instances. IDs as strings."""
    df = pd.read_sql_query(INSTANCE_QUERY, conn, params=[account_id])
    for col in ["campaign_id", "ad_group_id"]:
        df[col] = df[col].astype(str).str.strip()
    return df


# =====================================================================
# ROW BUILDERS (pause + create)
# =====================================================================

def build_pause_row(existing_rsa: pd.Series, columns: List[str]) -> Dict[str, str]:
    """Build a pause row from an existing RSA row. Preserve all original fields; flip status to Paused."""
    row = {col: "" for col in columns}
    # Copy whatever exists in the existing RSA row, mapped to our output column set
    for col in columns:
        if col in existing_rsa.index:
            val = existing_rsa[col]
            row[col] = "" if pd.isna(val) else str(val).strip()
    # Force status
    row["Ad status"] = "Paused"
    # Ad type field name varies; ensure populated
    if not row.get("Ad type"):
        row["Ad type"] = "Responsive search ad"
    return row


def build_create_row(
    campaign_name: str,
    campaign_id: str,
    ad_group_name: str,
    ad_group_id: str,
    inherited: Dict[str, str],
    headlines: List[str],
    descriptions: List[str],
    columns: List[str],
) -> Dict[str, str]:
    """Build a new RSA create row (Ad ID blank, all assets filled)."""
    row = {col: "" for col in columns}
    row["Ad status"]   = "Enabled"
    row["Campaign"]    = campaign_name
    row["Campaign ID"] = campaign_id
    row["Ad group"]    = ad_group_name
    row["Ad group ID"] = ad_group_id
    row["Ad ID"]       = ""  # new ad
    row["Ad type"]     = "Responsive search ad"
    for field in ALL_INHERITED_FIELDS:
        row[field] = inherited.get(field, "")
    # Pad headlines to 15
    padded_hls = headlines + [""] * (15 - len(headlines))
    for i, hl in enumerate(padded_hls[:15], start=1):
        row[f"Headline {i}"] = hl
        row[f"Headline {i} position"] = ""  # all Unpinned
    # Pad descriptions to 4
    padded_descs = descriptions + [""] * (4 - len(descriptions))
    for i, d in enumerate(padded_descs[:4], start=1):
        row[f"Description {i}"] = d
        row[f"Description {i} position"] = ""
    return row


# =====================================================================
# MAIN
# =====================================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db-path",       default=DEFAULT_DB_PATH)
    p.add_argument("--workbook",      default=DEFAULT_WORKBOOK)
    p.add_argument("--existing-rsas", default=DEFAULT_RSA_EXPORT)
    p.add_argument("--output-dir",    default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--account",       type=int, default=DEFAULT_ACCOUNT_ID)
    p.add_argument("--dry-run",       action="store_true",
                   help="Run all validation, do not write CSV")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    db_path       = Path(args.db_path)
    workbook_path = Path(args.workbook)
    rsa_export    = Path(args.existing_rsas)
    output_dir    = Path(args.output_dir)

    for p, label in [(db_path, "database"), (workbook_path, "workbook"), (rsa_export, "RSA export")]:
        if not p.exists():
            print(f"ERROR: {label} not found at {p}", file=sys.stderr)
            return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_date = date.today().isoformat()

    print(f"Database:      {db_path}")
    print(f"Workbook:      {workbook_path}")
    print(f"RSA export:    {rsa_export}")
    print(f"Output dir:    {output_dir.resolve()}")
    print(f"Account:       {args.account}")
    print(f"Dry-run:       {args.dry_run}")
    print()

    # Load all inputs
    print("Loading workbook copy ...")
    copy = load_workbook_copy(workbook_path)
    print(f"  UB headlines:    {len(copy['ub_headlines'])}")
    print(f"  UB descriptions: {len(copy['ub_descriptions'])}")
    print(f"  Intent sets:     {len(copy['intent_sets'])}")
    print(f"  Swaps ADD:       {len(copy['swaps_add'])}")

    print("Loading existing RSAs from export ...")
    existing = load_existing_rsas(rsa_export)
    print(f"  Active RSAs in export: {len(existing)}")

    print("Connecting to database ...")
    conn = sqlite3.connect(str(db_path))
    account_slug = get_account_slug(conn, args.account)
    print(f"  Account slug: {account_slug}")

    print("Querying active ad-group instances ...")
    instances = query_active_ad_group_instances(conn, args.account)
    print(f"  Active instances: {len(instances)}  ({instances['ad_group_name'].nunique()} unique names)")

    # Process each instance in-scope
    columns = get_output_columns()
    output_rows: List[Dict[str, str]] = []
    warnings: List[str] = []
    errors:   List[str] = []
    per_instance_log: List[str] = []

    pause_count = 0
    kept_count = 0
    create_count = 0
    excluded_count = 0
    skipped_no_existing = 0

    pool_reports: List[Dict[str, Any]] = []   # v11 review artifacts per instance
    kw_profile_cache: Dict[str, Dict[str, Any]] = {}

    for _, inst in instances.iterrows():
        ag_name      = inst["ad_group_name"]
        camp_name    = inst["campaign_name"]
        ag_id        = inst["ad_group_id"]
        camp_id      = inst["campaign_id"]

        design = LOGICAL_AD_GROUP_DESIGN.get(ag_name)
        if design is None:
            warnings.append(f"Logical ad group not in design dict: '{ag_name}' (in '{camp_name}')")
            continue

        tier = design["tier"]
        # Campaign-level tier override (e.g., CA distress mix for California campaign)
        if camp_name in CAMPAIGN_TIER_OVERRIDES:
            tier = CAMPAIGN_TIER_OVERRIDES[camp_name].get(tier, tier)
        if tier == "excluded":
            excluded_count += 1
            continue

        carrier = design.get("carrier")
        design_paths = design.get("paths")  # (Path1, Path2) or None

        # Find existing RSAs for this instance
        match = existing[
            (existing["Campaign ID"] == camp_id) &
            (existing["Ad group ID"] == ag_id)
        ]

        # Validate inheritance
        inherited, w, e = validate_url_path_inheritance(match, camp_name, ag_name, design_paths)
        warnings.extend(w)
        errors.extend(e)
        if len(match) == 0:
            skipped_no_existing += 1
            continue

        # Identify top performer; pause only lower performers (top RSA stays untouched
        # as the historical control alongside the new RSA).
        top_rsa = identify_top_rsa(match)
        top_ad_id = top_rsa["Ad ID"] if top_rsa is not None else None
        kept_count += 1 if top_ad_id else 0

        local_pauses = 0
        for _, exist_row in match.iterrows():
            if exist_row["Ad ID"] == top_ad_id:
                continue  # leave top RSA running as control
            output_rows.append(build_pause_row(exist_row, columns))
            pause_count += 1
            local_pauses += 1

        # Assemble new RSA copy (v11: keyword profile drives DKI band counting)
        if ag_id not in kw_profile_cache:
            kw_profile_cache[ag_id] = get_keyword_profile(conn, ag_id, carrier)
        kw_profile = kw_profile_cache[ag_id]

        headlines, descriptions, w2, e2, pool_report = assemble_copy_for_tier(
            tier, carrier, copy, kw_profile)
        pool_report["ad_group"] = ag_name
        pool_report["campaign"] = camp_name
        pool_report["kw_profile"] = kw_profile
        pool_reports.append(pool_report)
        warnings.extend([f"[{ag_name} / {camp_name}] {x}" for x in w2])
        errors.extend([f"[{ag_name} / {camp_name}] {x}" for x in e2])

        if e2 or e:
            per_instance_log.append(
                f"  SKIP create row: {camp_name} / {ag_name} ({tier})  [errors present]"
            )
            continue

        # Build create row
        output_rows.append(build_create_row(
            campaign_name=camp_name,
            campaign_id=camp_id,
            ad_group_name=ag_name,
            ad_group_id=ag_id,
            inherited=inherited,
            headlines=headlines,
            descriptions=descriptions,
            columns=columns,
        ))
        create_count += 1

        # Per-instance log: show kept vs paused for traceability
        top_id_short = top_ad_id[-6:] if top_ad_id else "?"
        per_instance_log.append(
            f"  {camp_name} / {ag_name} ({tier}, carrier={carrier or '-'}): "
            f"{local_pauses} pause + 1 keep (top=...{top_id_short}) + 1 create "
            f"({len(headlines)} HL / {len(descriptions)} desc)"
        )

    conn.close()

    # Print summary to stdout
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Pause rows generated:   {pause_count}  (lower-performer RSAs)")
    print(f"RSAs kept (no change):  {kept_count}   (top-performer per ad group, runs as control)")
    print(f"Create rows generated:  {create_count}")
    print(f"Excluded instances:     {excluded_count} (Group1 etc.)")
    print(f"Skipped (no existing):  {skipped_no_existing}")
    print(f"Warnings:               {len(warnings)}")
    print(f"Errors:                 {len(errors)}")
    print()

    # Write outputs
    csv_path    = output_dir / f"bulk_import_ads_{account_slug}_{snapshot_date}.csv"
    report_path = output_dir / f"bulk_import_ads_{account_slug}_{snapshot_date}_report.txt"

    # Validation report (always written)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"Bulk Import Generation Report -- Google Home Insurance\n")
        f.write(f"{'=' * 60}\n")
        f.write(f"Generated:    {datetime.now().isoformat()}\n")
        f.write(f"DB:           {db_path}\n")
        f.write(f"Workbook:     {workbook_path}\n")
        f.write(f"RSA export:   {rsa_export}\n")
        f.write(f"Account:      {args.account} ({account_slug})\n")
        f.write(f"Dry-run:      {args.dry_run}\n")
        f.write("\n")
        f.write(f"Pause rows:        {pause_count}  (lower-performer RSAs)\n")
        f.write(f"RSAs kept:         {kept_count}   (top per ad group, runs as control)\n")
        f.write(f"Create rows:       {create_count}\n")
        f.write(f"Excluded:          {excluded_count}\n")
        f.write(f"Skipped no-RSA:    {skipped_no_existing}\n")
        f.write(f"Warnings:          {len(warnings)}\n")
        f.write(f"Errors:            {len(errors)}\n")
        f.write("\n")
        f.write("PER-INSTANCE LOG\n")
        f.write("-" * 60 + "\n")
        for line in per_instance_log:
            f.write(line + "\n")

        # ---- v11: RUNG DISTRIBUTION PER CARRIER (primary review artifact) ----
        ladder_reports = [r for r in pool_reports if r.get("rungs")]
        if ladder_reports:
            f.write("\nRUNG DISTRIBUTION PER CARRIER (v11 ladder resolution)\n")
            f.write("-" * 60 + "\n")
            by_carrier: Dict[str, Dict[str, Any]] = {}
            for r in ladder_reports:
                key = r.get("carrier") or "?"
                # One representative per carrier (same carrier resolves identically);
                # count instances for context.
                entry = by_carrier.setdefault(key, {"rungs": r["rungs"], "n": 0, "tiers": set()})
                entry["n"] += 1
                entry["tiers"].add(r["tier"])
            for car in sorted(by_carrier):
                info = by_carrier[car]
                outcomes = list(info["rungs"].values())
                p1 = sum(1 for o in outcomes if str(o).startswith("P1"))
                p2 = sum(1 for o in outcomes if str(o).startswith("P2"))
                bare = sum(1 for o in outcomes if str(o) == "bare")
                dropped = sum(1 for o in outcomes if "dropped" in str(o) or "locked" in str(o))
                trimmed = sum(1 for o in outcomes if "trimmed" in str(o))
                f.write(f"\n  {car}  ({info['n']} instance(s), tiers: {', '.join(sorted(info['tiers']))})\n")
                f.write(f"    P1: {p1}   P2: {p2}   bare: {bare}   dropped/locked: {dropped}   ceiling-trimmed: {trimmed}\n")
                for concept, outcome in info["rungs"].items():
                    f.write(f"      {concept:<36} -> {outcome}\n")

        # ---- v11: POOL BAND SUMMARY ----
        if pool_reports:
            f.write("\nPOOL BAND SUMMARY (Home-naming band, floor "
                    f"{BAND_FLOOR_PCT:.0%} / soft ceiling {BAND_SOFT_CEILING})\n")
            f.write("-" * 60 + "\n")
            for r in pool_reports:
                if not r.get("n_headlines"):
                    continue
                kwp = r.get("kw_profile", {})
                f.write(
                    f"  {r['ad_group']} ({r['tier']}): {r['n_headlines']} HLs, "
                    f"Home {r['static_home']} ({r['home_pct']:.0%}, "
                    f"{r.get('brandfree_home', 0)} brand-free), "
                    f"bare {r['bare_count']}/{BARE_CAP}, "
                    f"families: {', '.join(r['families']) or '-'}\n"
                )
                f.write(f"      DKI: {r['dki_status']}  |  kw profile: {kwp.get('klass', '?')}"
                        f" (n={kwp.get('n', 0)}, home={kwp.get('home_frac')}, carrier={kwp.get('carrier_frac')})\n")
                for rem in r.get("remediations", []):
                    f.write(f"      ~ {rem}\n")

        # ---- v11: PHRASE DEDUPE DECISIONS ----
        all_dedupe = [(r["ad_group"], d) for r in pool_reports for d in r.get("dedupe", [])]
        if all_dedupe:
            f.write("\nPHRASE DEDUPE DECISIONS (one headline per distinctive phrase)\n")
            f.write("-" * 60 + "\n")
            seen_decisions: Dict[str, List[str]] = defaultdict(list)
            for ag, d in all_dedupe:
                seen_decisions[d].append(ag)
            for d, ags in sorted(seen_decisions.items()):
                f.write(f"  [{len(ags)}x] {d}\n")

        if warnings:
            f.write("\nWARNINGS (deduplicated)\n")
            f.write("-" * 60 + "\n")
            # Group warnings by their suffix (strip the instance prefix like "[X / Y] ").
            # A warning that fires N times across instances collapses to a single line with
            # a count and one example.
            dedup: Dict[str, Dict[str, Any]] = {}
            for w in warnings:
                # Strip leading instance prefix "[ad_group / campaign] " if present
                suffix = re.sub(r"^\[[^\]]+\]\s*", "", w)
                entry = dedup.setdefault(suffix, {"count": 0, "example": w})
                entry["count"] += 1
            for suffix, info in sorted(dedup.items(), key=lambda kv: -kv[1]["count"]):
                if info["count"] == 1:
                    f.write(f"  ! {info['example']}\n")
                else:
                    f.write(f"  ! [{info['count']}x] {suffix}\n")
                    f.write(f"        example: {info['example']}\n")
        if errors:
            f.write("\nERRORS (CSV NOT written)\n")
            f.write("-" * 60 + "\n")
            for e in errors:
                f.write(f"  X {e}\n")
    print(f"Report written: {report_path}")

    # Bulk CSV only if no errors AND not dry-run
    if errors:
        print(f"ERROR: {len(errors)} validation error(s). CSV NOT written. See report.", file=sys.stderr)
        return 1

    if args.dry_run:
        print("Dry-run: skipping CSV write.")
        return 0

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore", quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for r in output_rows:
            writer.writerow(r)
    print(f"CSV written:    {csv_path}")
    print(f"Total rows:     {len(output_rows)} (pause: {pause_count}, create: {create_count})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
