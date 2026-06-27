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
DEFAULT_WORKBOOK      = r"C:\Projects\google-ads-ops\ad_assets\exports\input\rsa-load-ready-copy_v8.xlsx" # r"C:\Projects\google-ads-ops\ad_assets\exports\input\rsa-load-ready-copy_v7.xlsx"
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
    "cq_full": {
        "intent_set":       "Carrier Conquesting (template)",
        "intent_set_hls":   "is:all",            # 7 CQ HLs (slots 1-7, all mention [Carrier])
        "intent_set_descs": "is:all",            # 2 CQ descs
        # Backbone (slots 8-15): DKI + Location first (keyword-adaptive), then themes
        "backbone_hls": [
            "ub:10",                             # {KeyWord:Home Insurance} -- DKI (keyword-adaptive)
            "ub:7",                              # {Location(State):...} -- Location (geo-adaptive)
            "ub:1",                              # We Make Insurers Compete -- Compare
            "ub:9",                              # Compare Carriers Side by Side -- Compare
            "ub:5",                              # Compare Top-Rated Insurers -- Social proof
            "ub:4",                              # Free, No-Obligation Quotes -- Trust
            "swaps:Compare Online, No Pressure", # Trust/differentiator
            "ub:3",                              # Get a Quote in 3 Minutes -- Speed
        ],
        "backbone_descs": ["ub:1", "ub:2"],
    },
    "cq_short": {
        "intent_set":       "Carrier Conquesting (template)",
        # Skip CQ1 (Side by Side) + CQ7 (Best Option) -- fail char limit for SF/Allied/21C
        "intent_set_hls":   ["is:2", "is:3", "is:4", "is:5", "is:6"],
        "intent_set_descs": "is:all",
        # Backbone (slots 6-15): DKI + Location first, then themes
        "backbone_hls": [
            "ub:10",                             # DKI
            "ub:7",                              # Location
            "ub:1",                              # Compare
            "ub:9",                              # Compare
            "ub:5",                              # Social proof
            "swaps:Highly Rated by Homeowners",  # Social proof
            "ub:4",                              # Trust
            "swaps:Compare Online, No Pressure", # Trust
            "ub:3",                              # Speed
            "swaps:Get Covered in 3 Minutes",    # Speed
        ],
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
        "intent_set_hls":   "is:all",                # 9 carrier HLs (H1-H9)
        "intent_set_descs": "is:all",
        "backbone_hls": [
            "ub:10",                                 # DKI (H10)
            "lit:Non-Renewed in California?",      # CA Distress (H11)
            "lit:CA Coverage Still Available",      # CA Distress (H12)
            "lit:Dropped by Your Insurer?",        # CA Distress (H13)
            "ub:4",                                  # Trust (H14)
            "ub:3",                                  # Speed (H15)
        ],
        "backbone_descs": ["ub:1", "ub:2"],
    },
    "cq_short_ca": {
        "intent_set":       "Carrier Conquesting (template)",
        "intent_set_hls":   ["is:2", "is:3", "is:4", "is:5", "is:6"],  # 5 carrier HLs
        "intent_set_descs": "is:all",
        "backbone_hls": [
            "ub:10",                                 # DKI (H6)
            "ub:7",                                  # Location (H7)
            "ub:1",                                  # Compare (H8)
            "lit:Non-Renewed in California?",      # CA Distress (H9)
            "lit:CA Coverage Still Available",      # CA Distress (H10)
            "lit:Dropped by Your Insurer?",        # CA Distress (H11)
            "lit:Insurer Left CA? Compare",        # CA Distress (H12)
            "lit:Non-Renewal? We Can Help",        # CA Distress (H13)
            "ub:4",                                  # Trust (H14)
            "ub:3",                                  # Speed (H15)
        ],
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

    return {
        "ub_headlines":    ub_headlines,
        "ub_descriptions": ub_descriptions,
        "intent_sets":     dict(intent_sets),
        "swaps_add":       swaps_add,
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
# COPY ASSEMBLY
# =====================================================================

def assemble_copy_for_tier(
    tier_name: str,
    carrier: Optional[str],
    copy: Dict[str, Any],
) -> Tuple[List[str], List[str], List[str], List[str]]:
    """
    Assemble headlines and descriptions for an RSA per the tier rules.

    Returns:
        (headlines, descriptions, warnings, errors)
    """
    warnings: List[str] = []
    errors: List[str] = []

    rules = TIER_RULES.get(tier_name) or CA_TIER_VARIANTS.get(tier_name)
    if rules is None:
        errors.append(f"Tier '{tier_name}' is excluded or unknown")
        return [], [], warnings, errors

    intent_set_name = rules.get("intent_set")

    # Expand and resolve headline refs (intent set first, then backbone)
    hl_refs: List[str] = []
    hl_refs += expand_intent_set_refs(rules.get("intent_set_hls"), intent_set_name, copy, "headlines")
    hl_refs += list(rules.get("backbone_hls") or [])

    headlines: List[str] = []
    for ref in hl_refs:
        try:
            text = resolve_headline_ref(ref, copy, intent_set_name)
        except (KeyError, ValueError) as e:
            errors.append(f"Resolve failure for headline {ref}: {e}")
            continue

        # Carrier substitution
        text = substitute_carrier(text, carrier)

        # Location insertion normalization
        text, changed = normalize_location_insertion(text)
        if changed:
            warnings.append(f"Location insertion normalized in headline: '{text}'")

        # Char limit check (post-substitution)
        n = visible_char_length(text)
        if n > HEADLINE_LIMIT:
            errors.append(f"Headline exceeds {HEADLINE_LIMIT} chars ({n}): '{text}'")

        # Belt-and-suspenders: ensure no [Carrier] token remains
        if "[Carrier]" in text:
            errors.append(f"Carrier substitution failed (placeholder remains): '{text}'")

        # Sanity: no double spaces
        if "  " in text:
            warnings.append(f"Double space in headline: '{text}'")

        headlines.append(text)

    # Expand and resolve description refs
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
        n = visible_char_length(text)
        if n > DESCRIPTION_LIMIT:
            errors.append(f"Description exceeds {DESCRIPTION_LIMIT} chars ({n}): '{text}'")
        if "[Carrier]" in text:
            errors.append(f"Carrier substitution failed in description: '{text}'")
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

    return headlines, descriptions, warnings, errors


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

        # Assemble new RSA copy
        headlines, descriptions, w2, e2 = assemble_copy_for_tier(tier, carrier, copy)
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
