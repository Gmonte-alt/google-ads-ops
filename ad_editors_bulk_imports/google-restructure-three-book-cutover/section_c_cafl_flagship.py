"""
section_c_cafl_flagship.py — Three-Book Restructure: Section C (CAFL Flagship)

Per builder_prompt_v2.md Section C.
Campaign: HOME INSURANCE-CALIFORNIA-NB-SEARCH-ALL-USA-STE (pre-rename)

Generates:
  section_c_keyword_adds_{date}.csv  — 25 flagship keyword adds to existing carrier groups
  section_c_geo_add_{date}.csv       — add Florida to CAFL geo targeting

Manual follow-up (do in Editor after import):
  C.2: Pause ALL Home-Generic-* ad groups
  C.2: Pause ALL Home-Non-Generic-Home_Group* ad groups
  C.3: Pause Home-Non-Generic-Home_CaRegion
  C.3: Pause Home-Non-Generic-Home_FlRegion (if present)
  C.4: Label all paused groups restr-2026-09-pause

Usage:
  python section_c_cafl_flagship.py --dry-run
  python section_c_cafl_flagship.py
"""

import csv
import os
import argparse
from datetime import datetime
from collections import defaultdict

OUTPUT_DIR = r"C:\Projects\google-ads-ops\ad_editors_bulk_imports\google-restructure-three-book-cutover\output\section_c"

CAMPAIGN = "HOME INSURANCE-CALIFORNIA-NB-SEARCH-ALL-USA-STE"

LABEL_ADD = "restr-2026-09-add"

# Florida geo ID for CAFL expansion
FL_GEO_ID = 21142

# ============================================================
# 25 Flagship keyword adds
# Routed to EXISTING carrier groups (no new groups created).
# Some keywords may already exist in these groups — Editor will show
# "no change" for duplicates. That's expected, not an error.
# ============================================================

FLAGSHIP_ADDS = [
    # Farmers — use existing -Flagship group (already has farmers insurance broad/exact)
    ("farmers insurance", "Broad", "Home-NonGeneric-Farmers Insurance-Flagship"),
    ("farmers insurance", "Exact", "Home-NonGeneric-Farmers Insurance-Flagship"),
    # Geico — use existing -Core group (already has geico broad)
    ("geico", "Broad", "Home-NonGeneric-Geico-Core"),
    ("geico condo insurance", "Exact", "Home-NonGeneric-Geico-Core"),
    ("geico landlord insurance", "Exact", "Home-NonGeneric-Geico-Core"),
    # Travelers — use existing -Core group
    ("travelers insurance", "Broad", "Home-NonGeneric-Travelers Insurance-Core"),
    ("travelers insurance", "Exact", "Home-NonGeneric-Travelers Insurance-Core"),
    ("travelers insurance company", "Exact", "Home-NonGeneric-Travelers Insurance-Core"),
    ("travelers", "Exact", "Home-NonGeneric-Travelers Insurance-Core"),
    # Kin — use existing -Core group
    ("kin insurance", "Exact", "Home-NonGeneric-Kin Insurance-Core"),
    ("kin insurance", "Broad", "Home-NonGeneric-Kin Insurance-Core"),
    ("kin insurance quote", "Exact", "Home-NonGeneric-Kin Insurance-Core"),
    ("kin insurance reviews", "Exact", "Home-NonGeneric-Kin Insurance-Core"),
    # Hartford — use existing -Core group
    ("hartford", "Broad", "Home-NonGeneric-Hartford Insurance-Core"),
    ("hartford insurance", "Exact", "Home-NonGeneric-Hartford Insurance-Core"),
    ("the hartford", "Exact", "Home-NonGeneric-Hartford Insurance-Core"),
    ("the hartford insurance company", "Exact", "Home-NonGeneric-Hartford Insurance-Core"),
    # Costco — use existing -Core group
    ("costco insurance", "Broad", "Home-NonGeneric-Costco Insurance-Core"),
    ("costco insurance", "Exact", "Home-NonGeneric-Costco Insurance-Core"),
    ("costco condo insurance", "Exact", "Home-NonGeneric-Costco Insurance-Core"),
    # American Family — use existing -Core group
    ("american family", "Exact", "Home-NonGeneric-American Family Insurance-Core"),
    ("american family insurance", "Exact", "Home-NonGeneric-American Family Insurance-Core"),
    ("american family insurance", "Broad", "Home-NonGeneric-American Family Insurance-Core"),
    # Universal — new group (no existing carrier group in CA)
    ("universal condo insurance", "Broad", "Home-NonGeneric-Universal Insurance-Core"),
    ("universal condo insurance", "Exact", "Home-NonGeneric-Universal Insurance-Core"),
]

# Known keywords that likely already exist in their target groups
# (from FL export which mirrors CA structure)
LIKELY_DUPLICATES = {
    ("farmers insurance", "Broad", "Home-NonGeneric-Farmers Insurance-Flagship"),
    ("farmers insurance", "Exact", "Home-NonGeneric-Farmers Insurance-Flagship"),
    ("geico", "Broad", "Home-NonGeneric-Geico-Core"),
    ("geico condo insurance", "Exact", "Home-NonGeneric-Geico-Core"),
    ("geico landlord insurance", "Exact", "Home-NonGeneric-Geico-Core"),
    ("travelers insurance", "Exact", "Home-NonGeneric-Travelers Insurance-Core"),
    ("travelers insurance", "Broad", "Home-NonGeneric-Travelers Insurance-Core"),
    ("travelers insurance company", "Exact", "Home-NonGeneric-Travelers Insurance-Core"),
    ("travelers", "Exact", "Home-NonGeneric-Travelers Insurance-Core"),
    ("kin insurance", "Exact", "Home-NonGeneric-Kin Insurance-Core"),
    ("kin insurance", "Broad", "Home-NonGeneric-Kin Insurance-Core"),
    ("kin insurance quote", "Exact", "Home-NonGeneric-Kin Insurance-Core"),
    ("kin insurance reviews", "Exact", "Home-NonGeneric-Kin Insurance-Core"),
    ("hartford", "Broad", "Home-NonGeneric-Hartford Insurance-Core"),
    ("hartford insurance", "Exact", "Home-NonGeneric-Hartford Insurance-Core"),
    ("the hartford", "Exact", "Home-NonGeneric-Hartford Insurance-Core"),
    ("the hartford insurance company", "Exact", "Home-NonGeneric-Hartford Insurance-Core"),
    ("costco insurance", "Broad", "Home-NonGeneric-Costco Insurance-Core"),
    ("costco insurance", "Exact", "Home-NonGeneric-Costco Insurance-Core"),
    ("costco condo insurance", "Exact", "Home-NonGeneric-Costco Insurance-Core"),
    ("american family", "Exact", "Home-NonGeneric-American Family Insurance-Core"),
    ("american family insurance", "Exact", "Home-NonGeneric-American Family Insurance-Core"),
    ("american family insurance", "Broad", "Home-NonGeneric-American Family Insurance-Core"),
}


def generate(output_dir, dry_run=False):
    os.makedirs(output_dir, exist_ok=True)
    ds = datetime.now().strftime("%Y-%m-%d")

    # Keyword add rows
    kw_rows = []
    for kw, mt, ag in FLAGSHIP_ADDS:
        kw_rows.append({
            "Campaign": CAMPAIGN,
            "Ad group": ag,
            "Keyword": kw,
            "Keyword match type": mt,
            "Keyword status": "Enabled",
            "Labels": LABEL_ADD,
        })

    # Geo add row
    geo_rows = [{
        "Campaign": CAMPAIGN,
        "ID": FL_GEO_ID,
        "Location": "Florida, United States",
        "Status": "Enabled",
    }]

    # Summary
    by_ag = defaultdict(list)
    new_count = 0
    dup_count = 0
    for kw, mt, ag in FLAGSHIP_ADDS:
        is_dup = (kw, mt, ag) in LIKELY_DUPLICATES
        by_ag[ag].append({"kw": kw, "mt": mt, "dup": is_dup})
        if is_dup:
            dup_count += 1
        else:
            new_count += 1

    print(f"\n{'='*70}")
    print("SECTION C — CAFL FLAGSHIP")
    print(f"Campaign: {CAMPAIGN}")
    print(f"{'='*70}")

    print(f"\n  Flagship keyword adds: {len(kw_rows)}")
    print(f"    Likely new: {new_count}")
    print(f"    Likely duplicate (Editor will show 'no change'): {dup_count}")

    for ag in sorted(by_ag.keys()):
        kws = by_ag[ag]
        existing = "EXISTS" if ag != "Home-NonGeneric-Universal Insurance-Core" else "NEW"
        print(f"\n    {ag} ({existing}):")
        for k in kws:
            flag = " ← likely exists" if k["dup"] else ""
            print(f"      {k['kw']:45} [{k['mt']:6}]{flag}")

    print(f"\n  Geo expansion: add Florida to CAFL targeting")

    print(f"\n{'='*70}")
    print("MANUAL FOLLOW-UP (do in Editor after import):")
    print("  C.2 Book purge — pause these ad groups:")
    print("    ALL Home-Generic-* groups (Cost-Core, Fire-Core, Flood-Core, Home Insurance-Core)")
    print("    ALL Home-Non-Generic-Home_Group* groups (Group1-6)")
    print("    Home-Non-Generic-Home_New")
    print("    Home-Non-Generic-Home_CaRegion")
    print("    Home-Non-Generic-Home_FlRegion (if present)")
    print("    ALL Renters groups")
    print("    Label all: restr-2026-09-pause")
    print()
    print("  Gate C check: only Home-NonGeneric-{Carrier}-Core/Flagship groups enabled")
    print("    Farmers Insurance-Core + Farmers Insurance-Flagship")
    print("    Geico-Core")
    print("    Hartford Insurance-Core")
    print("    Kin Insurance-Core")
    print("    Costco Insurance-Core")
    print("    Travelers Insurance-Core")
    print("    American Family Insurance-Core")
    print("    State Farm Insurance-Core")
    print("    Universal Insurance-Core (new)")

    if dry_run:
        print(f"\n{'='*70}")
        print("DRY RUN — no files written")
        return

    # Write keyword adds
    kw_path = os.path.join(output_dir, f"section_c_keyword_adds_{ds}.csv")
    kw_headers = ["Campaign", "Ad group", "Keyword", "Keyword match type", "Keyword status", "Labels"]
    with open(kw_path, "w", encoding="utf-16-le", newline="") as f:
        w = csv.DictWriter(f, fieldnames=kw_headers, delimiter="\t")
        w.writeheader()
        w.writerows(kw_rows)
    print(f"\n  Keyword file: {kw_path}")
    print(f"  ({len(kw_rows)} keyword adds)")

    # Write geo add
    geo_path = os.path.join(output_dir, f"section_c_geo_add_{ds}.csv")
    geo_headers = ["Campaign", "ID", "Location", "Status"]
    with open(geo_path, "w", encoding="utf-16-le", newline="") as f:
        w = csv.DictWriter(f, fieldnames=geo_headers, delimiter="\t")
        w.writeheader()
        w.writerows(geo_rows)
    print(f"  Geo file:     {geo_path}")

    print(f"\n{'='*70}")
    print("IMPORT ORDER:")
    print("  1. Import section_c_keyword_adds (25 flagship keywords)")
    print("  2. Import section_c_geo_add (add Florida to targeting)")
    print("  3. Manual: purge non-carrier groups per checklist above")
    print("  4. Verify: duplicates show 'no change', new keywords show 'add'")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Section C: CAFL Flagship Keyword Adds + Geo")
    p.add_argument("--output-dir", default=OUTPUT_DIR)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    generate(a.output_dir, a.dry_run)
