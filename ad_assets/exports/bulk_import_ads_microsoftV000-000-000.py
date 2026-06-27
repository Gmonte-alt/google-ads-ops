#!/usr/bin/env python3
"""
Microsoft Home Insurance - Bulk Import Generator for Microsoft Ads (RSAs)

Produces a single Microsoft Ads bulk CSV combining:
  1. PAUSE rows for the lower-performing RSA per ad group instance
  2. CREATE rows for new RSAs assembled per the locked creative design

Top-performing RSAs are LEFT UNTOUCHED (no row in the output CSV); they continue
running as the historical control alongside the new RSA.

INTENDED PATH
-------------
    C:\\Projects\\google-ads-ops\\ad_assets\\exports\\bulk_import_ads_microsoft.py

INPUTS
------
  --db-path       SQLite paid_search.db -- for confirming ad group instances
  --workbook      rsa-load-ready-copy_vN.xlsx -- copy library (shared with Google)
  --copy-csv      Microsoft RSA Ad report -- has Status, Campaign+Ad group NAMES,
                  headlines/descriptions, paths, URLs, and Ad ID. Title rows at top
                  (skiprows=2). Ad IDs come bracketed e.g. [84250643383355].
  --struct-csv    Microsoft structural report -- has Ad ID + Campaign ID + Ad group ID
                  (IDs that the copy CSV lacks). Title rows at top (skiprows=6).
                  Ad IDs duplicated by 'Ad distribution' (Search/Audience) -- dedup
                  on Ad ID. IDs bracketed.

  The two CSVs are joined on Ad ID. Microsoft cannot produce a single export with
  both copy AND structural IDs, hence the two-file pattern.

OUTPUTS
-------
  bulk_import_ads_microsoft_<account_slug>_<YYYY-MM-DD>.csv         -- bulk CSV
  bulk_import_ads_microsoft_<account_slug>_<YYYY-MM-DD>_report.txt  -- validation report

KEY DIFFERENCES FROM GOOGLE BULK FORMAT
---------------------------------------
- File starts with a 'Format Version' row (Name="6.0")
- Type column value is "Responsive Search Ad" (with spaces; Google uses
  "Responsive search ad")
- Status enum is Active / Paused (not Enabled / Paused)
- Microsoft RSA pause rows are minimal: just Type, Status, Id (no need to copy
  every field; omitted fields = unchanged per Microsoft bulk docs)
- Column names use spaces and Pascal case: 'Final Url', 'Tracking Template',
  'Mobile Final Url', 'Custom Parameter' (different casing than Google)
- Parent Id = ad group ID (links RSA to its ad group)
- Microsoft uses empty strings for "no value" (not "--" placeholder)

NOTE ON HEADLINE/DESCRIPTION FORMAT
-----------------------------------
Microsoft has TWO bulk formats:
  1. Bulk Service API (programmatic) -- uses JSON arrays in single Headline
     and Description columns
  2. Online Bulk Uploader (UI upload) -- uses FLAT columns (Headline 1 .. 15,
     Description 1 .. 4 with position columns), like Google

This script targets the Online Bulk Uploader (what operators use day-to-day).
Microsoft RSAs support 2-4 descriptions and 3-15 headlines (same as Google).

DESIGN
------
The locked creative design lives in LOGICAL_AD_GROUP_DESIGN + TIER_RULES below.
Copy text is sourced from the workbook (same workbook as Google):
  - "ub:N"               -- Universal Backbone sheet, headline/desc row N
  - "is:N" / "is:all"    -- Intent set within RSA Ad-Group Load Asset sheet
  - "swaps:<exact text>" -- Headline Swaps sheet ADD section, by exact match

MICROSOFT-SPECIFIC DESIGN ADJUSTMENTS (vs Google)
-------------------------------------------------
1. New tier `senior_focus` for the 4 senior-targeted ad groups (Microsoft has
   senior ad groups that Google does not). Senior swap headlines move to slots
   1-5 instead of being buried at slots 11-15 like backbone_plus does.
2. DKI (`ub:10`) placed first in backbone (slot 10 for cq_full, matching Google),
   ensuring it survives truncation when intent set contributes 9 headlines.
3. Path values normalized (no Geico exception); senior tier gets
   "Senior-Rates / Free-Quotes".

CONVENTIONS
-----------
- Read-only against paid_search.db (no INSERT / UPDATE / DELETE)
- Active-status-hierarchy filtering only (no last_seen_date)
- Account filter uses integer accounts.id (default 2 = Microsoft Home Insurance)
- Top RSA = highest Conversions, then CVR per impression, then Impressions,
  then Ad ID ascending (oldest = lowest ID, stable tiebreaker)
- Final Url / tracking / paths inherited from existing RSAs for high-stakes
  fields (hard-fail on conflict); paths come from design dict not inheritance
- Pinning honored from workbook (all Unpinned per current design)
- Location insertion syntax normalized to {Location(State):Your State}
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
    python bulk_import_ads_microsoft.py
    python bulk_import_ads_microsoft.py --copy-csv PATH --struct-csv PATH
    python bulk_import_ads_microsoft.py --dry-run     # validate without writing
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

DEFAULT_DB_PATH    = r"C:\Projects\google-ads-ops\db\paid_search.db"
DEFAULT_WORKBOOK   = r"C:\Projects\google-ads-ops\ad_assets\exports\input\rsa-load-ready-copy_v8.xlsx" # r"C:\Projects\google-ads-ops\ad_assets\exports\input\rsa-load-ready-copy_v7.xlsx"
DEFAULT_COPY_CSV   = r"C:\Projects\google-ads-ops\ad_assets\exports\input\Ad_report_microsoft.csv"
DEFAULT_STRUCT_CSV = r"C:\Projects\google-ads-ops\ad_assets\exports\input\Ad_structural_microsoft.csv"
DEFAULT_OUTPUT_DIR = r"C:\Projects\google-ads-ops\ad_assets\exports\output"
DEFAULT_ACCOUNT_ID = 2

HEADLINE_LIMIT    = 30
DESCRIPTION_LIMIT = 90

SHEET_UB          = "Universal Backbone"
SHEET_INTENT_SETS = "RSA Ad-Group Load Asset"
SHEET_SWAPS       = "Headline Swaps"

# Microsoft bulk format string constants
TYPE_FORMAT_VERSION   = "Format Version"
TYPE_RSA              = "Responsive Search Ad"
FORMAT_VERSION_VALUE  = "6.0"
STATUS_ACTIVE         = "Active"
STATUS_PAUSED         = "Paused"

# High-stakes URL/tracking fields (Microsoft column names: Pascal + spaces).
# New RSA inherits these from existing RSAs; conflicts are hard errors.
HIGH_STAKES_INHERITED_FIELDS = [
    "Final Url",
    "Mobile Final Url",
    "Tracking Template",
    "Final Url Suffix",
    "Custom Parameter",
]

# Path fields handled via design dict (not inheritance).
PATH_FIELDS = ["Path 1", "Path 2"]

ALL_INHERITED_FIELDS = HIGH_STAKES_INHERITED_FIELDS + PATH_FIELDS


# =====================================================================
# LOCKED DESIGN
# =====================================================================

LOGICAL_AD_GROUP_DESIGN: Dict[str, Dict[str, Any]] = {
    # ------------------------------------------------------------
    # cq_full -- 20 ad groups across 10 carriers (each <= 9 chars)
    # ------------------------------------------------------------
    # AAA (3) — membership/affinity, not carrier conquesting
    "Home-NonGeneric-AAA Insurance-Core-NA":           {"tier": "membership",    "carrier": "AAA",        "paths": ("Compare-Quotes",  "Free-Quotes")},
    "Home-NonGeneric-AAA Insurance-Quote-NA":          {"tier": "membership",    "carrier": "AAA",        "paths": ("Compare-Quotes",  "Free-Quotes")},
    "Home-NonGeneric-AAA Insurance-Service-NA":        {"tier": "membership",    "carrier": "AAA",        "paths": ("Compare-Quotes",  "Free-Quotes")},
    # Amica (5)
    "Home-NonGeneric-Amica Insurance-Core-NA":         {"tier": "cq_full",       "carrier": "Amica",      "paths": ("Compare-Quotes",  "Free-Quotes")},
    "Home-NonGeneric-Amica Insurance-Quote-NA":        {"tier": "cq_full",       "carrier": "Amica",      "paths": ("Compare-Quotes",  "Free-Quotes")},
    # Costco (2) — membership/affinity, not carrier conquesting
    "Home-NonGeneric-Costco Insurance-Core-NA":        {"tier": "membership",    "carrier": "Costco",     "paths": ("Compare-Quotes",  "Free-Quotes")},
    "Home-NonGeneric-Costco Insurance-Quote-NA":       {"tier": "membership",    "carrier": "Costco",     "paths": ("Compare-Quotes",  "Free-Quotes")},
    # Erie (4)
    "Home-NonGeneric-Erie Home Insurance-Core-NA":     {"tier": "cq_full",       "carrier": "Erie",       "paths": ("Compare-Quotes",  "Free-Quotes")},
    # Farmers (7)
    "Home-NonGeneric-Farmers Insurance-Core-NA":       {"tier": "cq_full",       "carrier": "Farmers",    "paths": ("Compare-Quotes",  "Free-Quotes")},
    # Geico (5)
    "Home-NonGeneric-Geico Insurance-Core-NA":         {"tier": "cq_full",       "carrier": "Geico",      "paths": ("Compare-Quotes",  "Free-Quotes")},
    "Home-NonGeneric-Geico Insurance-Quote-NA":        {"tier": "cq_full",       "carrier": "Geico",      "paths": ("Compare-Quotes",  "Free-Quotes")},
    "Home-NonGeneric-Geico Insurance-Compare-NA":      {"tier": "cq_full",       "carrier": "Geico",      "paths": ("Compare-Quotes",  "Free-Quotes")},
    # Hartford (8)
    "Home-NonGeneric-Hartford Home Insurance-Core-NA": {"tier": "cq_full",       "carrier": "Hartford",   "paths": ("Compare-Quotes",  "Free-Quotes")},
    # Mercury (7)
    "Home-NonGeneric-Mercury Insurance-Core-NA":       {"tier": "cq_full",       "carrier": "Mercury",    "paths": ("Compare-Quotes",  "Free-Quotes")},
    "Home-NonGeneric-Mercury Insurance-Quote-NA":      {"tier": "cq_full",       "carrier": "Mercury",    "paths": ("Compare-Quotes",  "Free-Quotes")},
    "Home-NonGeneric-Mercury Insurance-Service-NA":    {"tier": "cq_full",       "carrier": "Mercury",    "paths": ("Compare-Quotes",  "Free-Quotes")},
    # Travelers (9)
    "Home-NonGeneric-Travelers Insurance-Core-NA":     {"tier": "cq_full",       "carrier": "Travelers",  "paths": ("Compare-Quotes",  "Free-Quotes")},
    "Home-NonGeneric-Travelers Insurance-Quote-NA":    {"tier": "cq_full",       "carrier": "Travelers",  "paths": ("Compare-Quotes",  "Free-Quotes")},
    "Home-NonGeneric-Travelers Insurance-Service-NA":  {"tier": "cq_full",       "carrier": "Travelers",  "paths": ("Compare-Quotes",  "Free-Quotes")},
    # USAA (4)
    "Home-NonGeneric-USAA Insurance-Core-NA":          {"tier": "cq_full",       "carrier": "USAA",       "paths": ("Compare-Quotes",  "Free-Quotes")},

    # ------------------------------------------------------------
    # cq_short -- 3 ad groups (carriers at 10 chars; CQ1 & CQ7 fail)
    # ------------------------------------------------------------
    "Home-NonGeneric-Nationwide Insurance-Core-NA":    {"tier": "cq_short",      "carrier": "Nationwide", "paths": ("Compare-Quotes",  "Free-Quotes")},
    "Home-NonGeneric-Nationwide Insurance-Quote-NA":   {"tier": "cq_short",      "carrier": "Nationwide", "paths": ("Compare-Quotes",  "Free-Quotes")},
    "Home-NonGeneric-State Farm Insurance-Core-NA":    {"tier": "cq_short",      "carrier": "State Farm", "paths": ("Compare-Quotes",  "Free-Quotes")},

    # ------------------------------------------------------------
    # senior_focus -- 4 ad groups (NEW TIER, Microsoft-only)
    # ------------------------------------------------------------
    "Home-Generic-Senior Insurance-Core-NA":           {"tier": "senior_focus",  "carrier": None,         "paths": ("Senior-Rates",    "Free-Quotes")},
    "Home-Generic-Seniors-Core":                       {"tier": "senior_focus",  "carrier": None,         "paths": ("Senior-Rates",    "Free-Quotes")},
    "Home-Generic-Seniors-Misc":                       {"tier": "senior_focus",  "carrier": None,         "paths": ("Senior-Rates",    "Free-Quotes")},
    "Home-Generic-Seniors-New":                        {"tier": "senior_focus",  "carrier": None,         "paths": ("Senior-Rates",    "Free-Quotes")},

    # ------------------------------------------------------------
    # backbone_plus -- 12 ad groups (peril + generic + multi-brand)
    # ------------------------------------------------------------
    "Home-Generic-Fire Insurance-Core-NA":             {"tier": "backbone_plus", "carrier": None,         "paths": ("Fire-Insurance",  "Free-Quotes")},
    "Home-Generic-Flood Insurance-Core-NA":            {"tier": "backbone_plus", "carrier": None,         "paths": ("Flood-Insurance", "Free-Quotes")},
    "Home-Generic-Home Insurance":                     {"tier": "backbone_plus", "carrier": None,         "paths": ("Home-Insurance",  "Compare-Quotes")},
    "Home-Generic-Home Insurance-Cheapest":            {"tier": "backbone_plus", "carrier": None,         "paths": ("Home-Insurance",  "Compare-Quotes")},
    "Home-Generic-Home Insurance-Cheapest-NA":         {"tier": "backbone_plus", "carrier": None,         "paths": ("Home-Insurance",  "Compare-Quotes")},
    "Home-Generic-Home Insurance-Core":                {"tier": "backbone_plus", "carrier": None,         "paths": ("Home-Insurance",  "Compare-Quotes")},
    "Home-Generic-Home Insurance-Core-NA":             {"tier": "backbone_plus", "carrier": None,         "paths": ("Home-Insurance",  "Compare-Quotes")},
    "Home-Generic-Home Insurance-Core-Up":             {"tier": "backbone_plus", "carrier": None,         "paths": ("Home-Insurance",  "Compare-Quotes")},
    "Home-Generic-Insurance-Cheapest-NA":              {"tier": "backbone_plus", "carrier": None,         "paths": ("Home-Insurance",  "Compare-Quotes")},
    "Home-Generic-Insurance-Core-NA":                  {"tier": "backbone_plus", "carrier": None,         "paths": ("Home-Insurance",  "Compare-Quotes")},
    "Home-NonGeneric-Home Insurance-Group-1":          {"tier": "backbone_plus", "carrier": None,         "paths": ("Home-Insurance",  "Compare-Quotes")},
    "Home-NonGeneric-Home Insurance-Group-2":          {"tier": "backbone_plus", "carrier": None,         "paths": ("Home-Insurance",  "Compare-Quotes")},
}


# DKI placed first in backbone across all tiers, matching Google ordering.
# For senior_focus, DKI follows the 5 senior-themed headlines and precedes Location.
TIER_RULES: Dict[str, Optional[Dict[str, Any]]] = {
    "cq_full": {
        "intent_set":       "Carrier Conquesting (template)",
        "intent_set_hls":   "is:all",                          # 9 CQ HLs, slots 1-9
        "intent_set_descs": "is:all",                          # 2 CQ descs
        "backbone_hls": [
            "ub:10",                                           # DKI (slot 10, matching Google)
            "ub:7",                                            # Location (slot 11)
            "ub:1",                                            # Compare
            "ub:9",                                            # Compare
            "ub:5",                                            # Social proof
            "ub:4",                                            # Trust
            "swaps:Compare Online, No Pressure",               # Trust
            "ub:3",                                            # Speed (truncated if 9 intent HLs)
        ],
        "backbone_descs": ["ub:1", "ub:2"],
    },
    "membership": {
        # Membership/affinity brands (AAA, Costco). Reframes the member perk
        # as one option among many. Uses [member] placeholder instead of [Carrier].
        # Intent set already includes its own DKI ({KeyWord:Compare Rates} at row 7),
        # so backbone omits ub:10 to avoid duplicate DKI.
        "intent_set":       "Membership / Affinity",
        "intent_set_hls":   ["is:7", "is:8", "is:9",            # DKI + vertical anchors (keyword-relevant) first
                             "is:1", "is:2", "is:3",            # Angle headlines
                             "is:4", "is:5", "is:6"],           # Angle headlines
        "intent_set_descs": "is:all",                          # 2 membership descs
        "backbone_hls": [
            "ub:7",                                            # Location (slot 10)
            "ub:1",                                            # Compare
            "ub:9",                                            # Compare
            "ub:5",                                            # Social proof
            "ub:4",                                            # Trust
            "ub:3",                                            # Speed
        ],
        "backbone_descs": ["ub:1", "ub:2"],
    },
    "cq_short": {
        "intent_set":       "Carrier Conquesting (template)",
        # Skip CQ1 (Side by Side) + CQ7 (Best Option) -- 31 chars w/ 10-char carrier
        "intent_set_hls":   ["is:2", "is:3", "is:4", "is:5", "is:6"],
        "intent_set_descs": "is:all",
        "backbone_hls": [
            "ub:10",                                           # DKI (matching Google)
            "ub:7",                                            # Location
            "ub:1",                                            # Compare
            "ub:9",                                            # Compare
            "ub:5",                                            # Social
            "swaps:Highly Rated by Homeowners",                # Social
            "ub:4",                                            # Trust
            "swaps:Compare Online, No Pressure",               # Trust
            "ub:3",                                            # Speed
            "swaps:Get Covered in 3 Minutes",                  # Speed
        ],
        "backbone_descs": ["ub:1", "ub:2"],
    },
    "senior_focus": {
        # NEW TIER -- Microsoft only. Senior-themed HLs in slots 1-5
        # (Microsoft audit: senior intent strong but underweighted in current creative).
        "intent_set":       None,
        "intent_set_hls":   None,
        "intent_set_descs": None,
        "backbone_hls": [
            "swaps:Home Rates for Homeowners 50+",             # Senior (slot 1)
            "swaps:Mature Homeowner? Compare Now",             # Senior
            "swaps:Senior Homeowner Savings",                  # Senior
            "swaps:Compare 50+ Top Carriers",                  # Senior
            "ub:8",                                            # Cost-adjacent (Lower Your Home Premium)
            "ub:10",                                           # DKI (slot 6, before Location)
            "ub:7",                                            # Location (slot 7)
            "ub:1",                                            # Compare
            "ub:9",                                            # Compare
            "ub:5",                                            # Social
            "swaps:Highly Rated by Homeowners",                # Social
            "ub:4",                                            # Trust
            "swaps:Compare Online, No Pressure",               # Trust
            "ub:3",                                            # Speed
            "swaps:Get Covered in 3 Minutes",                  # Speed
        ],
        "backbone_descs": ["ub:1", "ub:2", "ub:3", "ub:4"],
    },
    "backbone_plus": {
        "intent_set":       None,
        "intent_set_hls":   None,
        "intent_set_descs": None,
        "backbone_hls": [
            "ub:10",                                           # DKI (slot 1, matching Google)
            "ub:7",                                            # Location (slot 2)
            "ub:1",                                            # Compare
            "ub:9",                                            # Compare
            "ub:5",                                            # Social
            "swaps:Highly Rated by Homeowners",                # Social
            "ub:4",                                            # Trust
            "swaps:Compare Online, No Pressure",               # Trust
            "ub:3",                                            # Speed
            "swaps:Get Covered in 3 Minutes",                  # Speed
            "ub:8",                                            # Cost (Lower Your Home Premium)
            "swaps:Home Rates for Homeowners 50+",             # Cost (senior)
            "swaps:Senior Homeowner Savings",                  # Cost (senior)
            "swaps:Mature Homeowner? Compare Now",             # Cost (senior)
            "swaps:Compare 50+ Top Carriers",                  # Cost (senior)
        ],
        "backbone_descs": ["ub:1", "ub:2", "ub:3", "ub:4"],
    },

    # Tiers below are kept for symmetry with Google but no Microsoft ad
    # groups currently use them. Add an entry in LOGICAL_AD_GROUP_DESIGN if
    # Microsoft adds an Aggregator or CA Non-Renewal ad group later.
    "aggregator": None,
    "ca_non_renewal": None,
    "excluded": None,
}


# =====================================================================
# HELPERS
# =====================================================================

def normalize_location_insertion(text: str) -> Tuple[str, bool]:
    """Normalize {LOCATION(X)} variants to {Location(X):Your State}."""
    original = text
    text = re.sub(
        r"\{LOCATION\(([^)]+)\)\}",
        lambda m: f"{{Location({m.group(1)}):Your State}}",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\{LOCATION\(([^)]+)\):([^}]*)\}",
        lambda m: f"{{Location({m.group(1)}):{m.group(2)}}}",
        text,
        flags=re.IGNORECASE,
    )
    return text, (text != original)


def substitute_carrier(text: str, carrier: Optional[str]) -> str:
    """Replace [Carrier] or [member] with carrier name; no-op if carrier is None."""
    if carrier is None:
        return text
    return text.replace("[Carrier]", carrier).replace("[member]", carrier)


def visible_char_length(text: str) -> int:
    """Length of text counting dynamic insertion fallbacks (not {...} tokens)."""
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
    """Microsoft Ads online Bulk Uploader column set for RSAs.

    IMPORTANT: Microsoft has TWO bulk formats:
      1. Bulk Service API (programmatic) -- uses JSON arrays in Headline + Description columns
      2. Online Bulk Uploader (UI upload) -- uses FLAT columns (Headline 1..15, Description 1..4)

    This script targets the Online Bulk Uploader (what Gilbert uses), so we use
    flat columns matching the format Microsoft's UI actually parses.

    Only the RSA-relevant subset of Microsoft's 300+ column schema is included.
    Other columns can be omitted; the uploader tolerates missing columns.
    """
    cols = [
        "Type",
        "Status",
        "Id",
        "Parent Id",
        "Campaign",
        "Ad Group",
        "Client Id",
        "Modified Time",
        "Ad Format Preference",
        "Name",
        "App Platform",
        "App Id",
        "Final Url",
        "Mobile Final Url",
        "Tracking Template",
        "Final Url Suffix",
        "Custom Parameter",
        "Path 1",
        "Path 2",
        "Domain",
    ]
    # 15 headlines × (text + position columns)
    for i in range(1, 16):
        cols.append(f"Headline {i}")
        cols.append(f"Headline {i} position")
    # 4 descriptions × (text + position columns)
    for i in range(1, 5):
        cols.append(f"Description {i}")
        cols.append(f"Description {i} position")
    return cols


# =====================================================================
# WORKBOOK LOADER (identical to Google)
# =====================================================================

def load_workbook_copy(workbook_path: Path) -> Dict[str, Any]:
    """Load UB + intent sets + Headline Swaps ADD section."""
    if not workbook_path.exists():
        raise FileNotFoundError(f"Workbook not found: {workbook_path}")

    # Universal Backbone -- detect header row dynamically
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

    # Intent sets
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

    # Headline Swaps ADD section
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
                if c0.lower() == "headline":
                    header_row_seen = True
                continue
            if not c0 or c0 == "nan":
                continue
            if c0.lower().startswith("note:") or c0.startswith("*"):
                continue
            if len(c0) > 40:
                continue
            swaps_add[c0] = c0

    return {
        "ub_headlines":    ub_headlines,
        "ub_descriptions": ub_descriptions,
        "intent_sets":     dict(intent_sets),
        "swaps_add":       swaps_add,
    }


# =====================================================================
# REFERENCE RESOLVER (identical to Google)
# =====================================================================

def resolve_headline_ref(ref: str, copy: Dict[str, Any], intent_set_name: Optional[str]) -> str:
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
    raise ValueError(f"Unrecognized headline reference: {ref}")


def resolve_description_ref(ref: str, copy: Dict[str, Any], intent_set_name: Optional[str]) -> str:
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


def assemble_copy_for_tier(
    tier_name: str,
    carrier: Optional[str],
    copy: Dict[str, Any],
) -> Tuple[List[str], List[str], List[str], List[str]]:
    warnings: List[str] = []
    errors: List[str] = []

    rules = TIER_RULES.get(tier_name)
    if rules is None:
        errors.append(f"Tier '{tier_name}' is excluded or not defined")
        return [], [], warnings, errors

    intent_set_name = rules.get("intent_set")

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
        text = substitute_carrier(text, carrier)
        text, changed = normalize_location_insertion(text)
        if changed:
            warnings.append(f"Location insertion normalized in headline: '{text}'")
        n = visible_char_length(text)
        if n > HEADLINE_LIMIT:
            errors.append(f"Headline exceeds {HEADLINE_LIMIT} chars ({n}): '{text}'")
        if "[Carrier]" in text or "[member]" in text:
            errors.append(f"Carrier/member substitution failed (placeholder remains): '{text}'")
        if "  " in text:
            warnings.append(f"Double space in headline: '{text}'")
        headlines.append(text)

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
        if "[Carrier]" in text or "[member]" in text:
            errors.append(f"Carrier/member substitution failed in description: '{text}'")
        descriptions.append(text)

    if len(headlines) < 3:
        errors.append(f"Too few headlines ({len(headlines)}); minimum is 3")
    if len(headlines) > 15:
        warnings.append(f"Too many headlines ({len(headlines)}); truncating to 15")
        headlines = headlines[:15]

    if len(descriptions) < 2:
        errors.append(f"Too few descriptions ({len(descriptions)}); minimum is 2")
    if len(descriptions) > 4:
        warnings.append(f"Too many descriptions ({len(descriptions)}); truncating to 4")
        descriptions = descriptions[:4]

    return headlines, descriptions, warnings, errors


# =====================================================================
# MICROSOFT EXISTING RSA LOADER (two CSVs joined on Ad ID)
# =====================================================================

def load_microsoft_existing_rsas(copy_csv: Path, struct_csv: Path) -> Tuple[pd.DataFrame, List[str]]:
    """Load Microsoft RSA exports: copy CSV (status + headlines) + structural CSV
    (Campaign ID, Ad group ID). Join on Ad ID; filter to active RSAs.

    Microsoft cannot produce a single export with both copy AND structural IDs.

    Returns:
        (joined_df, warnings_list)
        joined_df has all File 1 columns PLUS 'Campaign ID' and 'Ad group ID'
        (stripped of brackets).
    """
    warnings: List[str] = []

    if not copy_csv.exists():
        raise FileNotFoundError(f"Copy CSV not found: {copy_csv}")
    if not struct_csv.exists():
        raise FileNotFoundError(f"Structural CSV not found: {struct_csv}")

    # File 1: copy + status. Has 2 title rows before headers (skiprows=2). UTF-8 BOM.
    df1 = pd.read_csv(copy_csv, skiprows=2, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df1 = df1[(df1["Status"].str.strip() == "Enabled") &
              (df1["Ad type"].str.strip() == "ResponsiveSearch")].copy()
    df1["Ad ID"] = df1["Ad ID"].astype(str).str.strip("[]").str.strip()

    # Normalize "--" placeholder to empty (defensive -- Microsoft bulk format
    # expects empty strings, not "--" placeholders that the UI might emit).
    for col in HIGH_STAKES_INHERITED_FIELDS + PATH_FIELDS:
        if col in df1.columns:
            df1[col] = df1[col].astype(str).str.strip().replace("--", "")

    # File 2: structural IDs. Has 6 title rows before headers. UTF-8 BOM.
    df2 = pd.read_csv(struct_csv, skiprows=6, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df2 = df2[df2["Ad type"].str.strip() == "Responsive search ad"].copy()
    df2["Ad ID"]       = df2["Ad ID"].astype(str).str.strip("[]").str.strip()
    df2["Campaign ID"] = df2["Campaign ID"].astype(str).str.strip("[]").str.strip()
    df2["Ad group ID"] = df2["Ad group ID"].astype(str).str.strip("[]").str.strip()

    # Microsoft duplicates rows in the structural report by Ad distribution
    # (Search vs Audience). Dedup on Ad ID -- structural fields are identical
    # across distributions.
    df2_dedup = df2.drop_duplicates("Ad ID")[["Ad ID", "Campaign ID", "Ad group ID"]]

    # Join
    merged = df1.merge(df2_dedup, on="Ad ID", how="left")

    # Warn on missing joins (active RSAs in copy export with no struct match)
    missing_mask = merged["Campaign ID"].isna() | (merged["Campaign ID"] == "")
    n_missing = int(missing_mask.sum())
    if n_missing > 0:
        sample = merged[missing_mask]["Ad ID"].head(3).tolist()
        warnings.append(
            f"{n_missing} active RSAs in copy CSV have no matching structural "
            f"row in struct CSV (e.g., Ad IDs {sample}). These will be skipped."
        )
        merged = merged[~missing_mask].copy()

    return merged.reset_index(drop=True), warnings


# =====================================================================
# TOP RSA IDENTIFICATION (same logic as Google)
# =====================================================================

def identify_top_rsa(instance_rsas: pd.DataFrame) -> Optional[pd.Series]:
    """Top RSA: Conversions desc -> CVR/impr desc -> Impressions desc -> Ad ID asc.

    Microsoft uses 'Conv.' (with period) while Google uses 'Conversions'.
    Function checks both names defensively.
    """
    if len(instance_rsas) == 0:
        return None
    if len(instance_rsas) == 1:
        return instance_rsas.iloc[0]

    df = instance_rsas.copy()
    for col, sources in [("_impr", ["Impr.", "Impressions"]),
                         ("_conv", ["Conv.", "Conversions"])]:
        found = next((s for s in sources if s in df.columns), None)
        if found is None:
            df[col] = 0.0
        else:
            df[col] = pd.to_numeric(
                df[found].astype(str).str.replace(",", "").str.replace("--", "0").str.replace("<", ""),
                errors="coerce",
            ).fillna(0)
    df["_conv_per_impr"] = df["_conv"] / df["_impr"].replace(0, 1)
    df = df.sort_values(
        by=["_conv", "_conv_per_impr", "_impr", "Ad ID"],
        ascending=[False, False, False, True],
    )
    return df.iloc[0]


# =====================================================================
# URL/PATH INHERITANCE VALIDATION (same logic; Microsoft column names)
# =====================================================================

def validate_url_path_inheritance(
    instance_rsas: pd.DataFrame,
    campaign_name: str,
    ad_group_name: str,
    design_paths: Optional[Tuple[Optional[str], Optional[str]]],
) -> Tuple[Dict[str, str], List[str], List[str]]:
    """High-stakes fields hard-fail on conflict; paths come from design dict
    (fall back to inheritance with conflict detection if dict has none)."""
    warnings: List[str] = []
    errors: List[str] = []
    inherited: Dict[str, str] = {}

    if len(instance_rsas) == 0:
        errors.append(
            f"No existing RSAs found for {campaign_name} / {ad_group_name} -- "
            f"cannot inherit Final Url etc. Skipping."
        )
        return inherited, warnings, errors

    if len(instance_rsas) == 1:
        warnings.append(
            f"Only 1 existing RSA in {campaign_name} / {ad_group_name} (expected 2+). "
            f"Proceeding with single-RSA inheritance."
        )

    # High-stakes fields
    # NOTE: Filter out empty values BEFORE consensus check. After our first
    # upload's pause rows accidentally emptied Final URL on weaker RSAs (because
    # Microsoft's online Bulk Uploader treats blank URL cells as "set to empty",
    # not "unchanged" as docs claim), the data has mixed [URL, "", ""] values.
    # The top performer's URL is still intact and gives us the right consensus.
    for field in HIGH_STAKES_INHERITED_FIELDS:
        if field not in instance_rsas.columns:
            inherited[field] = ""
            continue
        values = instance_rsas[field].fillna("").astype(str).str.strip().tolist()
        non_empty = [v for v in values if v]
        unique_vals = list(set(non_empty))
        if len(unique_vals) == 1:
            inherited[field] = unique_vals[0]
        elif len(unique_vals) == 0:
            inherited[field] = ""  # All empty -- nothing to inherit (rare)
        else:
            errors.append(
                f"CONFLICT in {field} for {campaign_name} / {ad_group_name}: "
                f"{unique_vals!r}. Resolve in account before re-running."
            )
            inherited[field] = ""

    # Path fields -- design dict first, inheritance fallback
    if design_paths is not None and design_paths != (None, None):
        path1, path2 = design_paths
        inherited["Path 1"] = path1 or ""
        inherited["Path 2"] = path2 or ""
    else:
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
                    f"{unique_vals!r}. Add 'paths' to LOGICAL_AD_GROUP_DESIGN."
                )
                inherited[field] = ""

    return inherited, warnings, errors


# =====================================================================
# DATABASE QUERY (same as Google; platform-agnostic)
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
    df = pd.read_sql_query(INSTANCE_QUERY, conn, params=[account_id])
    for col in ["campaign_id", "ad_group_id"]:
        df[col] = df[col].astype(str).str.strip()
    return df


# =====================================================================
# ROW BUILDERS (Microsoft format)
# =====================================================================

def build_microsoft_pause_row(
    existing_rsa: pd.Series,
    inherited: Dict[str, str],
    columns: List[str],
) -> Dict[str, str]:
    """Microsoft pause row -- now includes inherited URL/tracking/path values.

    CRITICAL LESSON (learned the hard way): Microsoft's online Bulk Uploader
    treats EMPTY cells in URL/tracking/path columns as 'set to empty', NOT as
    'unchanged' as the Bulk Service API docs claim. A minimal pause row with
    blank Final Url / Tracking Template / Paths will ERASE those fields on the
    paused ad.

    We pass the consensus inherited values from the ad-group instance (same
    values used for the create row, since all RSAs in an instance share these).
    """
    row = {col: "" for col in columns}
    row["Type"]   = TYPE_RSA
    row["Status"] = STATUS_PAUSED
    row["Id"]     = str(existing_rsa["Ad ID"]).strip()
    for field in HIGH_STAKES_INHERITED_FIELDS + PATH_FIELDS:
        row[field] = inherited.get(field, "")
    return row


def build_microsoft_create_row(
    campaign_name: str,
    campaign_id: str,
    ad_group_name: str,
    ad_group_id: str,
    inherited: Dict[str, str],
    headlines: List[str],
    descriptions: List[str],
    columns: List[str],
) -> Dict[str, str]:
    """Microsoft create row: full RSA with FLAT Headline 1..15 / Description 1..4
    columns (online Bulk Uploader format -- NOT the JSON Headline/Description
    columns used by Bulk Service API)."""
    row = {col: "" for col in columns}
    row["Type"]                 = TYPE_RSA
    row["Status"]               = STATUS_ACTIVE
    row["Id"]                   = ""              # blank for new ad
    row["Parent Id"]            = ad_group_id     # links new ad to its ad group
    row["Campaign"]             = campaign_name
    row["Ad Group"]             = ad_group_name
    row["Ad Format Preference"] = "False"
    # Inherited URL/tracking + path fields
    for field in HIGH_STAKES_INHERITED_FIELDS + PATH_FIELDS:
        row[field] = inherited.get(field, "")
    # Flat headline columns: pad to 15, all positions blank (Unpinned per design)
    padded_hls = headlines + [""] * (15 - len(headlines))
    for i, hl in enumerate(padded_hls[:15], start=1):
        row[f"Headline {i}"] = hl
        row[f"Headline {i} position"] = ""
    # Flat description columns: pad to 4, all positions blank
    padded_descs = descriptions + [""] * (4 - len(descriptions))
    for i, d in enumerate(padded_descs[:4], start=1):
        row[f"Description {i}"] = d
        row[f"Description {i} position"] = ""
    return row


def write_microsoft_bulk_csv(rows: List[Dict[str, str]], columns: List[str], path: Path) -> None:
    """Write Microsoft bulk CSV with Format Version record as first data row."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore", quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        # Microsoft requires a Format Version record at the top
        fv = {col: "" for col in columns}
        fv["Type"] = TYPE_FORMAT_VERSION
        fv["Name"] = FORMAT_VERSION_VALUE
        writer.writerow(fv)
        for r in rows:
            writer.writerow(r)


# =====================================================================
# MAIN
# =====================================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db-path",     default=DEFAULT_DB_PATH)
    p.add_argument("--workbook",    default=DEFAULT_WORKBOOK)
    p.add_argument("--copy-csv",    default=DEFAULT_COPY_CSV,
                   help="Microsoft Ad report CSV (status + copy + Ad ID)")
    p.add_argument("--struct-csv",  default=DEFAULT_STRUCT_CSV,
                   help="Microsoft structural report CSV (Campaign ID + Ad group ID + Ad ID)")
    p.add_argument("--output-dir",  default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--account",     type=int, default=DEFAULT_ACCOUNT_ID)
    p.add_argument("--dry-run",     action="store_true",
                   help="Run validation, do not write bulk CSV")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    db_path       = Path(args.db_path)
    workbook_path = Path(args.workbook)
    copy_csv      = Path(args.copy_csv)
    struct_csv    = Path(args.struct_csv)
    output_dir    = Path(args.output_dir)

    for p, label in [(db_path, "database"), (workbook_path, "workbook"),
                     (copy_csv, "copy CSV"), (struct_csv, "structural CSV")]:
        if not p.exists():
            print(f"ERROR: {label} not found at {p}", file=sys.stderr)
            return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_date = date.today().isoformat()

    print(f"Database:      {db_path}")
    print(f"Workbook:      {workbook_path}")
    print(f"Copy CSV:      {copy_csv}")
    print(f"Struct CSV:    {struct_csv}")
    print(f"Output dir:    {output_dir.resolve()}")
    print(f"Account:       {args.account}")
    print(f"Dry-run:       {args.dry_run}")
    print()

    # Load workbook
    print("Loading workbook copy ...")
    copy = load_workbook_copy(workbook_path)
    print(f"  UB headlines:    {len(copy['ub_headlines'])}")
    print(f"  UB descriptions: {len(copy['ub_descriptions'])}")
    print(f"  Intent sets:     {len(copy['intent_sets'])}")
    print(f"  Swaps ADD:       {len(copy['swaps_add'])}")

    # Load existing RSAs (two CSVs joined)
    print("Loading existing RSAs (joining copy + structural exports) ...")
    existing, load_warnings = load_microsoft_existing_rsas(copy_csv, struct_csv)
    print(f"  Active RSAs after join: {len(existing)}")
    for w in load_warnings:
        print(f"  ! {w}")

    # Connect to DB
    print("Connecting to database ...")
    conn = sqlite3.connect(str(db_path))
    account_slug = get_account_slug(conn, args.account)
    print(f"  Account slug: {account_slug}")

    print("Querying active ad-group instances ...")
    instances = query_active_ad_group_instances(conn, args.account)
    print(f"  Active instances: {len(instances)}  ({instances['ad_group_name'].nunique()} unique names)")

    # Process each instance
    columns = get_output_columns()
    output_rows: List[Dict[str, str]] = []
    warnings: List[str] = list(load_warnings)
    errors:   List[str] = []
    per_instance_log: List[str] = []

    pause_count = 0
    kept_count  = 0
    create_count = 0
    excluded_count = 0
    skipped_no_existing = 0
    skipped_unmapped = 0

    for _, inst in instances.iterrows():
        ag_name   = inst["ad_group_name"]
        camp_name = inst["campaign_name"]
        ag_id     = inst["ad_group_id"]
        camp_id   = inst["campaign_id"]

        design = LOGICAL_AD_GROUP_DESIGN.get(ag_name)
        if design is None:
            warnings.append(
                f"Ad group not in design dict (skipped): '{ag_name}' (in '{camp_name}')"
            )
            skipped_unmapped += 1
            continue

        tier = design["tier"]
        if tier == "excluded":
            excluded_count += 1
            continue

        carrier      = design.get("carrier")
        design_paths = design.get("paths")

        match = existing[
            (existing["Campaign ID"] == camp_id) &
            (existing["Ad group ID"] == ag_id)
        ]

        # Use the CURRENT campaign + ad group names from Microsoft's data, not
        # the (possibly stale) names from the SQLite snapshot. Campaigns can
        # be renamed in Microsoft while keeping the same ID; if we write a
        # stale name in our CSV's Campaign column, the Editor doesn't match it
        # to the existing campaign and creates a phantom stub (with default
        # location targeting attached).
        if len(match) > 0:
            actual_camp_name = str(match.iloc[0]["Campaign"]).strip() or camp_name
            actual_ag_name   = str(match.iloc[0]["Ad group"]).strip() or ag_name
            if actual_camp_name != camp_name:
                warnings.append(
                    f"DB campaign name stale: '{camp_name}' (DB) "
                    f"-> '{actual_camp_name}' (Microsoft). Using current name."
                )
            if actual_ag_name != ag_name:
                warnings.append(
                    f"DB ad group name stale: '{ag_name}' (DB) "
                    f"-> '{actual_ag_name}' (Microsoft). Using current name."
                )
        else:
            actual_camp_name = camp_name
            actual_ag_name   = ag_name

        inherited, w, e = validate_url_path_inheritance(match, actual_camp_name, actual_ag_name, design_paths)
        warnings.extend(w)
        errors.extend(e)
        if len(match) == 0:
            skipped_no_existing += 1
            continue

        # Top RSA stays untouched; lower performers get pause rows
        top_rsa   = identify_top_rsa(match)
        top_ad_id = top_rsa["Ad ID"] if top_rsa is not None else None
        kept_count += 1 if top_ad_id else 0

        local_pauses = 0
        for _, exist_row in match.iterrows():
            if exist_row["Ad ID"] == top_ad_id:
                continue
            output_rows.append(build_microsoft_pause_row(exist_row, inherited, columns))
            pause_count  += 1
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

        output_rows.append(build_microsoft_create_row(
            campaign_name=actual_camp_name,
            campaign_id=camp_id,
            ad_group_name=actual_ag_name,
            ad_group_id=ag_id,
            inherited=inherited,
            headlines=headlines,
            descriptions=descriptions,
            columns=columns,
        ))
        create_count += 1

        top_id_short = top_ad_id[-6:] if top_ad_id else "?"
        per_instance_log.append(
            f"  {actual_camp_name} / {actual_ag_name} ({tier}, carrier={carrier or '-'}): "
            f"{local_pauses} pause + 1 keep (top=...{top_id_short}) + 1 create "
            f"({len(headlines)} HL / {len(descriptions)} desc)"
        )

    conn.close()

    # Summary to stdout
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Pause rows generated:    {pause_count}  (lower-performer RSAs)")
    print(f"RSAs kept (no change):   {kept_count}   (top per ad group; runs as control)")
    print(f"Create rows generated:   {create_count}")
    print(f"Excluded instances:      {excluded_count}")
    print(f"Skipped (no existing):   {skipped_no_existing}")
    print(f"Skipped (unmapped AG):   {skipped_unmapped}")
    print(f"Warnings:                {len(warnings)}")
    print(f"Errors:                  {len(errors)}")
    print()

    # Write outputs
    csv_path    = output_dir / f"bulk_import_ads_microsoft_{account_slug}_{snapshot_date}.csv"
    report_path = output_dir / f"bulk_import_ads_microsoft_{account_slug}_{snapshot_date}_report.txt"

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"Bulk Import Generation Report -- Microsoft Home Insurance\n")
        f.write(f"{'=' * 60}\n")
        f.write(f"Generated:    {datetime.now().isoformat()}\n")
        f.write(f"DB:           {db_path}\n")
        f.write(f"Workbook:     {workbook_path}\n")
        f.write(f"Copy CSV:     {copy_csv}\n")
        f.write(f"Struct CSV:   {struct_csv}\n")
        f.write(f"Account:      {args.account} ({account_slug})\n")
        f.write(f"Dry-run:      {args.dry_run}\n")
        f.write("\n")
        f.write(f"Pause rows:        {pause_count}  (lower-performer RSAs)\n")
        f.write(f"RSAs kept:         {kept_count}   (top per ad group; control)\n")
        f.write(f"Create rows:       {create_count}\n")
        f.write(f"Excluded:          {excluded_count}\n")
        f.write(f"Skipped no-RSA:    {skipped_no_existing}\n")
        f.write(f"Skipped unmapped:  {skipped_unmapped}\n")
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
            dedup: Dict[str, Dict[str, Any]] = {}
            for w in warnings:
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

    if errors:
        print(f"ERROR: {len(errors)} validation error(s). CSV NOT written. See report.", file=sys.stderr)
        return 1

    if args.dry_run:
        print("Dry-run: skipping CSV write.")
        return 0

    write_microsoft_bulk_csv(output_rows, columns, csv_path)
    print(f"CSV written:    {csv_path}")
    print(f"Total rows:     {len(output_rows) + 1} (1 Format Version + {pause_count} pause + {create_count} create)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
