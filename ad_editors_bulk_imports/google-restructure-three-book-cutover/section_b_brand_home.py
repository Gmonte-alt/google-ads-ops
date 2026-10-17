"""
section_b_brand_home.py — Three-Book Restructure: Section B (Brand-Home Campaign)

Per builder_prompt_v2.md Section B.
Campaign: HOME INSURANCE_STATE-GROUP_TIER2RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE

Generates:
  section_b_keyword_adds_{date}.csv — 28 brand-home transition keyword adds

Manual follow-up (do in Editor after import):
  B.7: Pause ALL Home-Generic-* ad groups (Cost-Core, Fire-Core, Flood-Core, Home Insurance-Core)
  B.7: Pause ALL Home-Non-Generic-Home_Group* ad groups (Group1-6)
  B.7: Leave Home-Non-Generic-Home_New as-is (NerdWallet lives here)
  B.7: Label all paused groups restr-2026-09-pause

Usage:
  python section_b_brand_home.py --dry-run
  python section_b_brand_home.py
"""

import csv
import os
import argparse
from datetime import datetime
from collections import defaultdict

OUTPUT_DIR = r"C:\Projects\google-ads-ops\ad_editors_bulk_imports\google-restructure-three-book-cutover\output\section_b"

CAMPAIGN = "HOME INSURANCE_STATE-GROUP_TIER2RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE"

LABEL_ADD = "restr-2026-09-add"

# ============================================================
# 28 Brand-Home transition adds (29 minus NerdWallet which stays in Home_New)
# Ad group mapping: existing groups where available, new groups for smaller carriers
# ============================================================

BRAND_HOME_ADDS = [
    # (keyword, match_type, ad_group)
    # American Home — new group
    ("american home insurance", "Broad", "Home-NonGeneric-American Home Insurance-Core"),
    ("american home insurance", "Exact", "Home-NonGeneric-American Home Insurance-Core"),
    # Bamboo — new group
    ("bamboo home insurance", "Broad", "Home-NonGeneric-Bamboo Insurance-Core"),
    ("bamboo home insurance", "Exact", "Home-NonGeneric-Bamboo Insurance-Core"),
    # Costco — existing group
    ("costco home and auto insurance", "Exact", "Home-NonGeneric-Costco Insurance-Core"),
    ("costco home insurance quote", "Exact", "Home-NonGeneric-Costco Insurance-Core"),
    # CSAA — new group
    ("csaa home insurance", "Broad", "Home-NonGeneric-CSAA Insurance-Core"),
    ("csaa home insurance", "Exact", "Home-NonGeneric-CSAA Insurance-Core"),
    # Farmers — existing group
    ("farmers homeowners insurance", "Broad", "Home-NonGeneric-Farmers Insurance-Core"),
    # Geico — existing group (note: Geico-Core, NOT Geico Insurance-Core)
    ("geico home insurance quote", "Exact", "Home-NonGeneric-Geico-Core"),
    ("geico homeowners insurance california", "Exact", "Home-NonGeneric-Geico-Core"),
    # Hartford — existing group
    ("hartford home insurance california", "Broad", "Home-NonGeneric-Hartford Insurance-Core"),
    ("hartford home insurance california", "Exact", "Home-NonGeneric-Hartford Insurance-Core"),
    ("hartford home insurance texas", "Broad", "Home-NonGeneric-Hartford Insurance-Core"),
    ("hartford homeowners insurance", "Exact", "Home-NonGeneric-Hartford Insurance-Core"),
    # Hochheim Prairie — new group
    ("hochheim prairie home insurance", "Broad", "Home-NonGeneric-Hochheim Prairie Insurance-Core"),
    ("hochheim prairie home insurance", "Exact", "Home-NonGeneric-Hochheim Prairie Insurance-Core"),
    # Homeowners of America — new group
    ("homeowners of america home insurance", "Broad", "Home-NonGeneric-Homeowners of America-Core"),
    # Mercury — new group
    ("mercury home insurance", "Broad", "Home-NonGeneric-Mercury Insurance-Core"),
    ("mercury home insurance", "Exact", "Home-NonGeneric-Mercury Insurance-Core"),
    # Plymouth Rock — new group
    ("plymouth rock home insurance", "Broad", "Home-NonGeneric-Plymouth Rock Insurance-Core"),
    ("plymouth rock home insurance", "Exact", "Home-NonGeneric-Plymouth Rock Insurance-Core"),
    # Travelers — existing group
    ("travelers home insurance california", "Broad", "Home-NonGeneric-Travelers Insurance-Core"),
    ("travelers home insurance florida", "Broad", "Home-NonGeneric-Travelers Insurance-Core"),
    ("travelers home insurance texas", "Broad", "Home-NonGeneric-Travelers Insurance-Core"),
    ("travelers homeowners insurance", "Broad", "Home-NonGeneric-Travelers Insurance-Core"),
    # Wawanesa — new group
    ("wawanesa home insurance", "Broad", "Home-NonGeneric-Wawanesa Insurance-Core"),
    ("wawanesa home insurance", "Exact", "Home-NonGeneric-Wawanesa Insurance-Core"),
]

# Skipped: nerdwallet home insurance [Exact] — stays in Home-Non-Generic-Home_New


def generate(output_dir, dry_run=False):
    os.makedirs(output_dir, exist_ok=True)
    ds = datetime.now().strftime("%Y-%m-%d")

    rows = []
    for kw, mt, ag in BRAND_HOME_ADDS:
        rows.append({
            "Campaign": CAMPAIGN,
            "Ad group": ag,
            "Keyword": kw,
            "Keyword match type": mt,
            "Keyword status": "Enabled",
            "Labels": LABEL_ADD,
        })

    # Summary
    by_ag = defaultdict(list)
    for kw, mt, ag in BRAND_HOME_ADDS:
        by_ag[ag].append(f"{kw} [{mt}]")

    # Identify existing vs new groups
    existing_groups = {
        "Home-NonGeneric-Costco Insurance-Core",
        "Home-NonGeneric-Farmers Insurance-Core",
        "Home-NonGeneric-Geico-Core",
        "Home-NonGeneric-Hartford Insurance-Core",
        "Home-NonGeneric-Travelers Insurance-Core",
        "Home-NonGeneric-Kin Insurance-Core",
        "Home-NonGeneric-State Farm Insurance-Core",
        "Home-NonGeneric-American Family Insurance-Core",
    }

    print(f"\n{'='*70}")
    print("SECTION B — BRAND-HOME CAMPAIGN")
    print(f"Campaign: {CAMPAIGN}")
    print(f"{'='*70}")

    print(f"\n  Keyword adds: {len(rows)}")
    for ag in sorted(by_ag.keys()):
        kws = by_ag[ag]
        status = "EXISTS" if ag in existing_groups else "NEW"
        print(f"\n    {ag} ({status}):")
        for kw in kws:
            print(f"      {kw}")

    print(f"\n  Skipped: nerdwallet home insurance [Exact] — stays in Home_New")

    print(f"\n{'='*70}")
    print("MANUAL FOLLOW-UP (do in Editor after import):")
    print("  B.7 Book purge — pause these ad groups in Brand-Home:")
    print("    Home-Generic-Cost-Core")
    print("    Home-Generic-Fire-Core")
    print("    Home-Generic-Flood-Core")
    print("    Home-Generic-Home Insurance-Core")
    print("    Home-Non-Generic-Home_Group1 through Home_Group6")
    print("    Home-Non-Generic-Home_CaRegion (if present)")
    print("    ⚠ KEEP Home-Non-Generic-Home_New ENABLED (NerdWallet)")
    print("    Label all paused groups: restr-2026-09-pause")
    print()
    print("  Gate B check: only Home-NonGeneric-{Carrier}-Core groups + Home_New enabled")

    if dry_run:
        print(f"\n{'='*70}")
        print("DRY RUN — no files written")
        return

    path = os.path.join(output_dir, f"section_b_keyword_adds_{ds}.csv")
    headers = ["Campaign", "Ad group", "Keyword", "Keyword match type", "Keyword status", "Labels"]
    with open(path, "w", encoding="utf-16-le", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, delimiter="\t")
        w.writeheader()
        w.writerows(rows)

    print(f"\n  File: {path}")
    print(f"  ({len(rows)} keyword adds, UTF-16 LE tab-delimited)")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Section B: Brand-Home Campaign Keyword Adds")
    p.add_argument("--output-dir", default=OUTPUT_DIR)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    generate(a.output_dir, a.dry_run)
