"""
google_three_book_cutover.py — Google Home Three-Book Restructure

Per: builder_prompt.md (2026-09-08), rulings D7=MOVE, 1C=NATIONAL-MINUS-CA/FL.
Source of truth: rollout_plan.xlsx + keyword_book_assignment_FROZEN_2026-08-27.csv.

Outputs (Google Ads Editor, UTF-16 LE tab-delimited):
  Phase 1: keyword_adds_{date}.csv         — all transition/launch keyword creates
           keyword_moves_d7_{date}.csv      — existing TX generics → themed groups (D7=MOVE)
  Phase 2: campaign_pause_{date}.csv        — retire FL, TIER1, TIER3, TIER4
  Phase 3: campaign_settings_{date}.csv     — renames + flagship enable/geo

Negatives handled separately per Gilbert's direction.

Usage:
  python google_three_book_cutover.py --dry-run
  python google_three_book_cutover.py
"""

import csv
import os
import argparse
from datetime import datetime
from collections import defaultdict

OUTPUT_DIR = r"C:\Projects\google-ads-ops\ad_editors_bulk_imports\google-restructure-three-book-cutover\output"

# ============================================================
# CAMPAIGN IDS & NAMES
# ============================================================

CAMPAIGNS = {
    "TX":    {"id": "23394808297", "name": "HOME INSURANCE-TEXAS-NB-SEARCH-ALL-USA-STE"},
    "TIER2": {"id": "23530860942", "name": "HOME INSURANCE_STATE-GROUP_TIER2RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE"},
    "CA":    {"id": "23385188622", "name": "HOME INSURANCE-CALIFORNIA-NB-SEARCH-ALL-USA-STE"},
    # NATIONAL FLAGSHIP — new campaign, created manually in Editor before keyword import
    "NATL_FLAG": {"id": "NEW", "name": "HOME INSURANCE_FLAGSHIP_NB_TROAS_SEARCH_ALLDEVICE_USA-STE"},
    "FL":    {"id": "23394808300", "name": "HOME INSURANCE-FLORIDA-NB-SEARCH-ALL-USA-STE"},
    "TIER1": {"id": "23496334752", "name": "HOME INSURANCE_STATE-GROUP_Tier1RPL80Plus_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE"},
    "TIER3": {"id": "23586637678", "name": "HOME INSURANCE_STATE-GROUP_Tier3RPL50Plus_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE"},
    "TIER4": {"id": "23428389081", "name": "HOME INSURANCE_STATE-GROUP_TIER4_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE"},
}

NEW_NAMES = {
    "TX":        "HOME INSURANCE-TEXAS-NB-SEARCH-ALL-USA-STE",  # unchanged
    "TIER2":     "HOME INSURANCE_STATE-GROUP_TIER2_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",  # brand-home
    "CA":        "HOME INSURANCE_FLAGSHIP_CAFL_TROAS_SEARCH_ALLDEVICE_USA-STE",  # CA/FL flagship
    "NATL_FLAG": "HOME INSURANCE_FLAGSHIP_NB_TROAS_SEARCH_ALLDEVICE_USA-STE",  # national flagship (new)
}

RETIRE = {
    "FL":    "zz-RETIRED-HOME INSURANCE-FLORIDA-NB-SEARCH-ALL-USA-STE",
    "TIER1": "zz-RETIRED-HOME INSURANCE_STATE-GROUP_TIER1_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
    "TIER3": "zz-RETIRED-HOME INSURANCE_STATE-GROUP_TIER3_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
    "TIER4": "zz-RETIRED-HOME INSURANCE_STATE-GROUP_TIER4_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
}

# Struck rows (9/8 ruling)
STRUCK = {
    ("citzen home insurance quote", "EXACT"),
    ("universial property and casualty", "BROAD"),
}

# ============================================================
# PHASE 1A: GENERIC TRANSITION ADDS (21 keywords → TX themed groups)
# ============================================================

GENERIC_ADDS = [
    # (keyword, match_type, ad_group)
    # CORE — use existing ad group "Home-Generic-Home Insurance-Core"
    # NOTE: These 3 keywords likely already exist in this group. Verify before import;
    # if already present, remove these rows to avoid duplicates.
    ("home insurance", "Exact", "Home-Generic-Home Insurance-Core"),
    ("homeowners insurance", "Broad", "Home-Generic-Home Insurance-Core"),
    ("homeowners insurance", "Exact", "Home-Generic-Home Insurance-Core"),
    # QUOTE
    ("homeowners insurance quote", "Exact", "Home-Generic-Quote"),
    # FIRE (new ad group — fire-specific peril keywords)
    ("fire insurance", "Broad", "Home-Generic-Fire"),
    ("fire insurance", "Exact", "Home-Generic-Fire"),
    ("fire insurance california", "Broad", "Home-Generic-Fire"),
    ("fire insurance california", "Exact", "Home-Generic-Fire"),
    ("fire insurance in california", "Exact", "Home-Generic-Fire"),
    ("california home insurance in high fire risk areas", "Exact", "Home-Generic-Fire"),
    ("home insurance in high fire risk areas", "Exact", "Home-Generic-Fire"),
    # FLOOD — use existing ad group "Home-Generic-Flood-Core"
    ("flood insurance", "Broad", "Home-Generic-Flood-Core"),
    ("florida flood insurance", "Broad", "Home-Generic-Flood-Core"),
    # STATE
    ("california home insurance", "Broad", "Home-Generic-State"),
    ("california home insurance", "Exact", "Home-Generic-State"),
    ("homeowners insurance california", "Exact", "Home-Generic-State"),
    ("colorado home insurance", "Broad", "Home-Generic-State"),
    ("florida home insurance", "Broad", "Home-Generic-State"),
    ("utah home insurance", "Broad", "Home-Generic-State"),
    # OTHER
    ("home insurance calculator by zip code", "Phrase", "Home-Generic-Other"),
    # COMPARE (wave-parked)
    ("best home insurance", "Phrase", "Home-Generic-Compare"),
]

# ============================================================
# PHASE 1A (D7=MOVE): EXISTING TX GENERICS → THEMED GROUPS
# These keywords already exist in TX — pause old instance, add in new themed group.
# Source: K1T sheet, filtered to source_campaigns containing TEXAS.
# ============================================================

TX_EXISTING_MOVES = [
    # (keyword, match_type, new_ad_group) — current instance gets paused
    # QUOTE
    ("home insurance quotes", "Broad", "Home-Generic-Quote"),
    ("home insurance quotes", "Exact", "Home-Generic-Quote"),
    ("homeowners insurance quote", "Broad", "Home-Generic-Quote"),
    ("home insurance estimate", "Phrase", "Home-Generic-Quote"),
    # FIRE (was Peril — fire keywords only)
    ("fire insurance quote", "Broad", "Home-Generic-Fire"),
    ("fire insurance coverage", "Broad", "Home-Generic-Fire"),
    ("fire insurance in california", "Broad", "Home-Generic-Fire"),
    ("california home insurance in high fire risk areas", "Broad", "Home-Generic-Fire"),
    ("home insurance in high fire risk areas", "Broad", "Home-Generic-Fire"),
    # STATE
    ("florida home insurance", "Exact", "Home-Generic-State"),
    ("homeowners insurance california", "Broad", "Home-Generic-State"),
    # CORE — use existing "Home-Generic-Home Insurance-Core"
    ("home insurance", "Broad", "Home-Generic-Home Insurance-Core"),
    # COST — existing "Home-Generic-Cost-Core" (already exists, not in rollout plan)
    ("cheapest home insurance", "Phrase", "Home-Generic-Cost-Core"),
    # OTHER
    ("homeowners insurance quotes", "Broad", "Home-Generic-Other"),
    ("homeowners insurance quotes", "Exact", "Home-Generic-Other"),
    ("homeowner insurance", "Broad", "Home-Generic-Other"),
    ("homeowner insurance", "Exact", "Home-Generic-Other"),
    ("home owners insurance", "Broad", "Home-Generic-Other"),
    ("home owners insurance", "Exact", "Home-Generic-Other"),
    ("home insurance quote", "Exact", "Home-Generic-Other"),
    ("homeowners insurance quotes online", "Broad", "Home-Generic-Other"),
    ("home insurance rates", "Broad", "Home-Generic-Other"),
    ("home insurance near me", "Broad", "Home-Generic-Other"),
    ("home insurance companies", "Exact", "Home-Generic-Other"),
    ("home insurance companies near me", "Broad", "Home-Generic-Other"),
    ("how much is homeowners insurance", "Exact", "Home-Generic-Other"),
    ("home owners insurance quotes", "Broad", "Home-Generic-Other"),
    ("compare home insurance", "Broad", "Home-Generic-Other"),
    ("compare home insurance", "Exact", "Home-Generic-Other"),
    ("home insurance cost", "Broad", "Home-Generic-Other"),
    ("homeowners insurance cost", "Broad", "Home-Generic-Other"),
    ("cheap home insurance", "Broad", "Home-Generic-Other"),
    ("cheap home insurance", "Exact", "Home-Generic-Other"),
    ("home insurance online", "Broad", "Home-Generic-Other"),
    ("cheapest home insurance", "Broad", "Home-Generic-Other"),
    ("cheapest home insurance in california", "Broad", "Home-Generic-Other"),
    ("cheap homeowners insurance", "Broad", "Home-Generic-Other"),
    # COMPARE
    ("best home insurance", "Broad", "Home-Generic-Compare"),
]

# ============================================================
# PHASE 1B: BRAND-HOME TRANSITION ADDS (29 keywords → TIER2 carrier groups)
# ============================================================

# Carrier ad group mapping derived from keyword text
def derive_carrier_group(keyword):
    """Map keyword to carrier ad group based on brand name in text."""
    kw = keyword.lower()
    mapping = [
        ("geico", "Home-NonGeneric-Geico Insurance-Core"),
        ("farmers", "Home-NonGeneric-Farmers Insurance-Core"),
        ("travelers", "Home-NonGeneric-Travelers Insurance-Core"),
        ("travellers", "Home-NonGeneric-Travelers Insurance-Core"),
        ("hartford", "Home-NonGeneric-Hartford Insurance-Core"),
        ("kin ", "Home-NonGeneric-Kin Insurance-Core"),
        ("costco", "Home-NonGeneric-Costco Insurance-Core"),
        ("american family", "Home-NonGeneric-American Family Insurance-Core"),
        ("amica", "Home-NonGeneric-Amica Insurance-Core"),
        ("erie", "Home-NonGeneric-Erie Insurance-Core"),
        ("state farm", "Home-NonGeneric-State Farm Insurance-Core"),
        ("nationwide", "Home-NonGeneric-Nationwide Insurance-Core"),
        ("mercury", "Home-NonGeneric-Mercury Insurance-Core"),
        ("hippo", "Home-NonGeneric-Hippo Insurance-Core"),
        ("liberty", "Home-NonGeneric-Liberty Insurance-Core"),
        ("elephant", "Home-NonGeneric-Elephant Insurance-Core"),
        ("assurant", "Home-NonGeneric-Assurant Insurance-Core"),
        ("insurify", "Home-NonGeneric-Insurify Insurance-Core"),
        ("the general", "Home-NonGeneric-The General Insurance-Core"),
        ("tgs", "Home-NonGeneric-TGS Insurance-Core"),
        ("homesite", "Home-NonGeneric-Homesite Insurance-Core"),
        ("metlife", "Home-NonGeneric-MetLife Insurance-Core"),
        ("aaa", "Home-NonGeneric-AAA Insurance-Core"),
        ("triple a", "Home-NonGeneric-AAA Insurance-Core"),
        ("nerdwallet", "Home-NonGeneric-NerdWallet-Core"),
        ("bamboo", "Home-NonGeneric-Bamboo Insurance-Core"),
        ("csaa", "Home-NonGeneric-CSAA Insurance-Core"),
        ("hochheim", "Home-NonGeneric-Hochheim Prairie Insurance-Core"),
        ("plymouth rock", "Home-NonGeneric-Plymouth Rock Insurance-Core"),
        ("wawanesa", "Home-NonGeneric-Wawanesa Insurance-Core"),
        ("homeowners of america", "Home-NonGeneric-Homeowners of America-Core"),
        ("american home", "Home-NonGeneric-American Home Insurance-Core"),
        ("citizens", "Home-NonGeneric-Citizens Insurance-Core"),
    ]
    for token, group in mapping:
        if token in kw:
            return group
    return "UNMAPPED"


BRAND_HOME_ADDS = [
    # (keyword, match_type) — ad group derived from keyword text
    ("american home insurance", "Broad"),
    ("american home insurance", "Exact"),
    ("bamboo home insurance", "Broad"),
    ("bamboo home insurance", "Exact"),
    ("costco home and auto insurance", "Exact"),
    ("costco home insurance quote", "Exact"),
    ("csaa home insurance", "Broad"),
    ("csaa home insurance", "Exact"),
    ("farmers homeowners insurance", "Broad"),
    ("geico home insurance quote", "Exact"),
    ("geico homeowners insurance california", "Exact"),
    ("hartford home insurance california", "Broad"),
    ("hartford home insurance california", "Exact"),
    ("hartford home insurance texas", "Broad"),
    ("hartford homeowners insurance", "Exact"),
    ("hochheim prairie home insurance", "Broad"),
    ("hochheim prairie home insurance", "Exact"),
    ("homeowners of america home insurance", "Broad"),
    ("mercury home insurance", "Broad"),
    ("mercury home insurance", "Exact"),
    ("nerdwallet home insurance", "Exact"),
    ("plymouth rock home insurance", "Broad"),
    ("plymouth rock home insurance", "Exact"),
    ("travelers home insurance california", "Broad"),
    ("travelers home insurance florida", "Broad"),
    ("travelers home insurance texas", "Broad"),
    ("travelers homeowners insurance", "Broad"),
    ("wawanesa home insurance", "Broad"),
    ("wawanesa home insurance", "Exact"),
]

# ============================================================
# PHASE 1C: FLAGSHIP LAUNCH (25 keywords → California/Flagship carrier groups)
# ============================================================

def derive_flagship_group(keyword):
    """Map flagship keyword to existing carrier ad group in California campaign.
    Uses Home-NonGeneric-{Carrier}-Flagship convention (matching existing Farmers group).
    Keywords like 'geico' [BROAD] may already exist in the -Core variant — verify before import."""
    kw = keyword.lower()
    mapping = [
        ("farmers", "Home-NonGeneric-Farmers Insurance-Flagship"),
        ("geico", "Home-NonGeneric-Geico-Flagship"),
        ("travelers", "Home-NonGeneric-Travelers Insurance-Flagship"),
        ("travellers", "Home-NonGeneric-Travelers Insurance-Flagship"),
        ("kin ", "Home-NonGeneric-Kin Insurance-Flagship"),
        ("kin com", "Home-NonGeneric-Kin Insurance-Flagship"),
        ("hartford", "Home-NonGeneric-Hartford Insurance-Flagship"),
        ("costco", "Home-NonGeneric-Costco Insurance-Flagship"),
        ("american family", "Home-NonGeneric-American Family Insurance-Flagship"),
        ("universal", "Home-NonGeneric-Universal Insurance-Flagship"),
    ]
    for token, group in mapping:
        if token in kw:
            return group
    return "UNMAPPED"


FLAGSHIP_LAUNCH = [
    ("farmers insurance", "Broad"), ("farmers insurance", "Exact"),
    ("geico", "Broad"), ("geico condo insurance", "Exact"), ("geico landlord insurance", "Exact"),
    ("travelers insurance", "Broad"), ("travelers insurance", "Exact"),
    ("travelers insurance company", "Exact"), ("travelers", "Exact"),
    ("kin insurance", "Exact"), ("kin insurance", "Broad"),
    ("kin insurance quote", "Exact"), ("kin insurance reviews", "Exact"),
    ("hartford", "Broad"), ("hartford insurance", "Exact"),
    ("the hartford", "Exact"), ("the hartford insurance company", "Exact"),
    ("costco insurance", "Broad"), ("costco insurance", "Exact"), ("costco condo insurance", "Exact"),
    ("american family", "Exact"), ("american family insurance", "Exact"),
    ("american family insurance", "Broad"),
    ("universal condo insurance", "Broad"), ("universal condo insurance", "Exact"),
]

# Google Ads geo criterion IDs for US states
GEO_IDS = {
    "Alabama": 21133, "Alaska": 21132, "Arizona": 21136, "Arkansas": 21135,
    "California": 21137, "Colorado": 21138, "Connecticut": 21139, "Delaware": 21141,
    "Florida": 21142, "Georgia": 21143, "Hawaii": 21144, "Idaho": 21146,
    "Illinois": 21147, "Indiana": 21148, "Iowa": 21145, "Kansas": 21149,
    "Kentucky": 21150, "Louisiana": 21151, "Maine": 21154, "Maryland": 21153,
    "Massachusetts": 21152, "Michigan": 21155, "Minnesota": 21156, "Mississippi": 21158,
    "Missouri": 21157, "Montana": 21159, "Nebraska": 21162, "Nevada": 21166,
    "New Hampshire": 21163, "New Jersey": 21164, "New Mexico": 21165, "New York": 21167,
    "North Carolina": 21160, "North Dakota": 21161, "Ohio": 21168, "Oklahoma": 21169,
    "Oregon": 21170, "Pennsylvania": 21171, "Rhode Island": 21172,
    "South Carolina": 21173, "South Dakota": 21174, "Tennessee": 21175, "Texas": 21176,
    "Utah": 21177, "Vermont": 21179, "Virginia": 21178, "Washington": 21180,
    "West Virginia": 21183, "Wisconsin": 21182, "Wyoming": 21184,
}

# CA/FL Flagship: add FL to existing CA geo
CAFL_GEO_ADD = [("Florida", 21142)]

# National Flagship: exclude CA + FL
NATL_FLAGSHIP_EXCLUDE = [("California", 21137), ("Florida", 21142)]

# All 50 states for national flagship (minus CA/FL = 48 states)
ALL_STATES_MINUS_CAFL = {k: v for k, v in GEO_IDS.items() if k not in ("California", "Florida")}


def generate(output_dir, dry_run=False):
    os.makedirs(output_dir, exist_ok=True)
    ds = datetime.now().strftime("%Y-%m-%d")

    # ============================================================
    # PHASE 1: KEYWORD ADDS
    # ============================================================

    kw_rows = []

    # 1A: Generic adds to TX
    for kw, mt, ag in GENERIC_ADDS:
        kw_rows.append({
            "Campaign": CAMPAIGNS["TX"]["name"],
            "Ad group": ag,
            "Keyword": kw,
            "Keyword match type": mt,
            "Keyword status": "Enabled",
        })

    # 1B: Brand-home adds to TIER2
    for kw, mt in BRAND_HOME_ADDS:
        ag = derive_carrier_group(kw)
        kw_rows.append({
            "Campaign": CAMPAIGNS["TIER2"]["name"],
            "Ad group": ag,
            "Keyword": kw,
            "Keyword match type": mt,
            "Keyword status": "Enabled",
        })

    # 1C: Flagship launch — keywords go to BOTH campaigns
    # CA/FL Flagship (existing CA campaign, uses existing carrier groups)
    for kw, mt in FLAGSHIP_LAUNCH:
        ag = derive_flagship_group(kw)
        kw_rows.append({
            "Campaign": CAMPAIGNS["CA"]["name"],
            "Ad group": ag,
            "Keyword": kw,
            "Keyword match type": mt,
            "Keyword status": "Enabled",
        })

    # National Flagship (new campaign, same carrier groups)
    for kw, mt in FLAGSHIP_LAUNCH:
        ag = derive_flagship_group(kw)
        kw_rows.append({
            "Campaign": CAMPAIGNS["NATL_FLAG"]["name"],
            "Ad group": ag,
            "Keyword": kw,
            "Keyword match type": mt,
            "Keyword status": "Enabled",
        })

    # D7 MOVE: existing TX keywords — add new instance in themed group
    d7_add_rows = []
    d7_pause_rows = []
    for kw, mt, ag in TX_EXISTING_MOVES:
        d7_add_rows.append({
            "Campaign": CAMPAIGNS["TX"]["name"],
            "Ad group": ag,
            "Keyword": kw,
            "Keyword match type": mt,
            "Keyword status": "Enabled",
        })
        # Pause rows need the OLD ad group — we don't have that here,
        # so these will need to be done in the Editor by filtering the
        # original ad groups after the new instances are created.
        d7_pause_rows.append({
            "Campaign": CAMPAIGNS["TX"]["name"],
            "Keyword": kw,
            "Keyword match type": mt,
            "Note": "Pause OLD instance in original ad group after themed instance is created",
        })

    # ============================================================
    # PHASE 2: CAMPAIGN PAUSES
    # ============================================================

    pause_rows = []
    for key in ["FL", "TIER1", "TIER3", "TIER4"]:
        pause_rows.append({
            "Campaign#Original": CAMPAIGNS[key]["name"],
            "Campaign": RETIRE[key],
            "Campaign Status": "Paused",
        })

    # ============================================================
    # PHASE 3: CAMPAIGN SETTINGS
    # ============================================================

    settings_rows = []
    # Rename TIER2 → brand-home book name
    settings_rows.append({
        "Campaign#Original": CAMPAIGNS["TIER2"]["name"],  # TIER2RPL (current)
        "Campaign": NEW_NAMES["TIER2"],
        "Campaign Status": "",
    })
    # Rename CA → flagship
    settings_rows.append({
        "Campaign#Original": CAMPAIGNS["CA"]["name"],
        "Campaign": NEW_NAMES["CA"],
        "Campaign Status": "",
    })

    # Flagship geo setup
    flagship_geo_rows = []

    # CA/FL Flagship: add Florida to existing California campaign
    for state, geo_id in CAFL_GEO_ADD:
        flagship_geo_rows.append({
            "Campaign": CAMPAIGNS["CA"]["name"],
            "ID": geo_id,
            "Location": f"{state}, United States",
            "Status": "Enabled",
        })

    # National Flagship: add all states, then exclude CA + FL
    # (Adding all states as Enabled targets, then CA/FL as Excluded)
    natl_geo_rows = []
    for state, geo_id in sorted(ALL_STATES_MINUS_CAFL.items()):
        natl_geo_rows.append({
            "Campaign": CAMPAIGNS["NATL_FLAG"]["name"],
            "ID": geo_id,
            "Location": f"{state}, United States",
            "Status": "Enabled",
        })

    # ============================================================
    # SUMMARY
    # ============================================================

    print(f"\n{'='*70}")
    print("GOOGLE HOME THREE-BOOK RESTRUCTURE — BUILD SUMMARY")
    print(f"{'='*70}")

    print(f"\n  PHASE 1A — Generic adds to TX: {len([r for r in kw_rows if r['Campaign'] == CAMPAIGNS['TX']['name']])}")
    ag_counts = defaultdict(int)
    for r in kw_rows:
        if r['Campaign'] == CAMPAIGNS['TX']['name']:
            ag_counts[r['Ad group']] += 1
    for ag in sorted(ag_counts):
        print(f"    {ag}: {ag_counts[ag]} keywords")

    print(f"\n  PHASE 1A (D7=MOVE) — Existing TX → themed groups: {len(d7_add_rows)} adds")
    d7_ag = defaultdict(int)
    for r in d7_add_rows:
        d7_ag[r['Ad group']] += 1
    for ag in sorted(d7_ag):
        print(f"    {ag}: {d7_ag[ag]} keywords")

    print(f"\n  PHASE 1B — Brand-home adds to TIER2: {len([r for r in kw_rows if r['Campaign'] == CAMPAIGNS['TIER2']['name']])}")
    bh_ag = defaultdict(int)
    for r in kw_rows:
        if r['Campaign'] == CAMPAIGNS['TIER2']['name']:
            bh_ag[r['Ad group']] += 1
    for ag in sorted(bh_ag):
        print(f"    {ag}: {bh_ag[ag]} keywords")

    print(f"\n  PHASE 1C — Flagship launch to CA: {len([r for r in kw_rows if r['Campaign'] == CAMPAIGNS['CA']['name']])}")
    fl_ag = defaultdict(int)
    for r in kw_rows:
        if r['Campaign'] == CAMPAIGNS['CA']['name']:
            fl_ag[r['Ad group']] += 1
    for ag in sorted(fl_ag):
        print(f"    {ag}: {fl_ag[ag]} keywords")

    print(f"\n  PHASE 2 — Campaign pauses: {len(pause_rows)}")
    for r in pause_rows:
        print(f"    {r['Campaign#Original'][:50]}... → Paused")

    print(f"\n  PHASE 3 — Campaign renames: {len(settings_rows)}")
    for r in settings_rows:
        print(f"    {r['Campaign#Original'][:45]}...")
        print(f"    → {r['Campaign']}")

    print(f"\n  Flagship geo:")
    print(f"    CA/FL Flagship: add Florida to existing CA campaign")
    print(f"    National Flagship: {len(ALL_STATES_MINUS_CAFL)} states (all minus CA/FL)")

    # Totals
    total_kw = len(kw_rows)
    total_d7 = len(d7_add_rows)
    print(f"\n  TOTALS:")
    print(f"    Keyword creates: {total_kw} (21 generic + 29 brand-home + 25 flagship)")
    print(f"    D7 themed moves: {total_d7} (add new + pause old in TX)")
    print(f"    Campaign pauses: {len(pause_rows)}")
    print(f"    Campaign renames: {len(settings_rows)}")

    unmapped = [r for r in kw_rows if 'UNMAPPED' in r.get('Ad group', '')]
    if unmapped:
        print(f"\n  ⚠ UNMAPPED KEYWORDS ({len(unmapped)}):")
        for r in unmapped:
            print(f"    {r['Keyword']} [{r['Keyword match type']}] → UNMAPPED")

    if dry_run:
        print(f"\n{'='*70}")
        print("DRY RUN — no files written")
        return

    # ============================================================
    # WRITE FILES
    # ============================================================

    # Phase 1: Keyword adds
    kw_path = os.path.join(output_dir, f"keyword_adds_{ds}.csv")
    kw_headers = ["Campaign", "Ad group", "Keyword", "Keyword match type", "Keyword status"]
    with open(kw_path, "w", encoding="utf-16-le", newline="") as f:
        w = csv.DictWriter(f, fieldnames=kw_headers, delimiter="\t")
        w.writeheader()
        w.writerows(kw_rows)
    print(f"\n  keyword_adds: {kw_path}")

    # Phase 1 D7: Themed group adds
    d7_path = os.path.join(output_dir, f"keyword_moves_d7_{ds}.csv")
    with open(d7_path, "w", encoding="utf-16-le", newline="") as f:
        w = csv.DictWriter(f, fieldnames=kw_headers, delimiter="\t")
        w.writeheader()
        w.writerows(d7_add_rows)
    print(f"  keyword_moves_d7: {d7_path}")

    # Phase 2: Campaign pauses
    pause_path = os.path.join(output_dir, f"campaign_pause_{ds}.csv")
    pause_headers = ["Campaign#Original", "Campaign", "Campaign Status"]
    with open(pause_path, "w", encoding="utf-16-le", newline="") as f:
        w = csv.DictWriter(f, fieldnames=pause_headers, delimiter="\t")
        w.writeheader()
        w.writerows(pause_rows)
    print(f"  campaign_pause: {pause_path}")

    # Phase 3: Campaign settings
    settings_path = os.path.join(output_dir, f"campaign_settings_{ds}.csv")
    with open(settings_path, "w", encoding="utf-16-le", newline="") as f:
        w = csv.DictWriter(f, fieldnames=pause_headers, delimiter="\t")
        w.writeheader()
        w.writerows(settings_rows)
    print(f"  campaign_settings: {settings_path}")

    # Flagship geo — CA/FL add
    geo_path = os.path.join(output_dir, f"flagship_cafl_geo_{ds}.csv")
    geo_headers = ["Campaign", "ID", "Location", "Status"]
    with open(geo_path, "w", encoding="utf-16-le", newline="") as f:
        w = csv.DictWriter(f, fieldnames=geo_headers, delimiter="\t")
        w.writeheader()
        w.writerows(flagship_geo_rows)
    print(f"  flagship_cafl_geo: {geo_path}")

    # National flagship geo — 48 state targets
    natl_geo_path = os.path.join(output_dir, f"flagship_national_geo_{ds}.csv")
    with open(natl_geo_path, "w", encoding="utf-16-le", newline="") as f:
        w = csv.DictWriter(f, fieldnames=geo_headers, delimiter="\t")
        w.writeheader()
        w.writerows(natl_geo_rows)
    print(f"  flagship_national_geo: {natl_geo_path}")

    print(f"\n{'='*70}")
    print("IMPORT ORDER:")
    print("  1. Create national flagship campaign shell in Editor (name, tROAS 1.10, budget)")
    print("  2. keyword_adds (creates carrier groups in both flagships + generic/brand-home adds)")
    print("  3. keyword_moves_d7 (adds themed instances of existing TX generics)")
    print("  4. In Editor: pause OLD instances of D7 keywords in original TX ad groups")
    print("  5. flagship_cafl_geo (add FL to CA/FL flagship)")
    print("  6. flagship_national_geo (48 state targets for national flagship)")
    print("  7. campaign_pause (retire FL, TIER1, TIER3, TIER4)")
    print("  8. campaign_settings (rename TIER2 + CA)")
    print("  9. Set tROAS 1.10 on TX, TIER2, CA/FL flagship, and national flagship")
    print("  10. Set budgets per plan workbook")
    print("  11. Verify: no stale settings on CA from legacy life")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Google Home Three-Book Restructure Cutover")
    p.add_argument("--output-dir", default=OUTPUT_DIR)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    generate(a.output_dir, a.dry_run)
