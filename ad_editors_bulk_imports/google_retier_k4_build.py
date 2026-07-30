"""
google_retier_k4_build.py — Google Home RPL k=4 Re-Tier (single cutover)

Per: RPL_ReTier_Build_Packet_2026-07-29.md
Scope: 26 state placements across 4 shells. CA/TX/FL untouched.

Outputs (Google Ads Editor format, UTF-16 LE tab-delimited):
  1. retier_locations_{date}.csv   — all location adds/removes across 4 shells
  2. retier_campaign_{date}.csv    — renames + T4 enable (import AFTER locations)

Manual steps NOT in this script (do in Editor before posting):
  - F1: Detach T1 shell from portfolio "Tier 2 - Combined" → campaign-level tROAS 115%
  - F2: Detach T4 shell from portfolio "Tier 1 - Combined" → campaign-level tROAS 112%
  - T4 budget set per plan workbook

Usage:
  python google_retier_k4_build.py --dry-run
  python google_retier_k4_build.py
"""

import csv
import os
import argparse
from datetime import datetime
from collections import defaultdict

OUTPUT_DIR = r"C:\Projects\google-ads-ops\ad_editors_bulk_imports\output"

# ============================================================
# CURRENT STATE (from ads_editor_locations_2026-07-29_pre-cutover.csv)
# ============================================================

CURRENT_NAMES = {
    "T1": "HOME INSURANCE_STATE-GROUP_Tier1RPL80Plus_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
    "T2": "HOME INSURANCE_STATE-GROUP_Tier2RPL70Plus_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
    "T3": "HOME INSURANCE_STATE-GROUP_Tier3RPL50Plus_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
    "T4": "zz-RETIRED-HOME INSURANCE-TIER ONE-LEGACY-NB-SEARCH-ALL-USA-STE",
}

NEW_NAMES = {
    "T1": "HOME INSURANCE_STATE-GROUP_TIER1_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
    "T2": "HOME INSURANCE_STATE-GROUP_TIER2_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
    "T3": "HOME INSURANCE_STATE-GROUP_TIER3_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
    "T4": "HOME INSURANCE_STATE-GROUP_TIER4_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
}

# Google Ads geo criterion IDs
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

# ============================================================
# CHANGE SET (from build packet)
# ============================================================

CHANGES = {
    "T1": {
        "remove": ["Alabama", "Delaware", "Ohio", "Oregon", "Utah", "Wisconsin"],
        "add":    ["Illinois", "Kansas", "Massachusetts", "New Hampshire", "New Jersey", "New Mexico"],
        # Keep: Minnesota (anchor)
        # End state: IL, KS, MA, MN, NH, NJ, NM (7 states)
    },
    "T2": {
        "remove": ["Illinois", "Kansas", "Maryland", "New Hampshire", "New Jersey",
                    "New Mexico", "Oklahoma", "Pennsylvania"],
        "add":    ["Colorado", "Kentucky", "Ohio", "Utah"],
        # Keep: AZ, AR, GA, ID, MO, NC, TN (7 anchors)
        # End state: AZ, AR, CO, GA, ID, KY, MO, NC, OH, TN, UT (11 states)
    },
    "T3": {
        "remove": ["Colorado", "Hawaii", "Kentucky", "Louisiana", "Massachusetts",
                    "Mississippi", "Nevada", "New York", "Rhode Island",
                    "South Carolina", "South Dakota", "Wyoming"],
        "add":    ["Alabama", "Maryland", "Oklahoma", "Oregon", "Pennsylvania", "Wisconsin"],
        # Keep: CT, IN, IA, MI, NE, VA, WA, WV (8 anchors)
        # End state: AL, CT, IN, IA, MD, MI, NE, OK, OR, PA, VA, WA, WI, WV (14 states)
    },
    "T4": {
        # Strict sequence: strip stale → keep existing → add new
        "strip_stale": ["Connecticut", "Colorado", "Virginia"],
        "keep":        ["Hawaii", "Louisiana", "Nevada", "New York", "South Carolina"],
        "add":         ["Delaware", "Mississippi", "Rhode Island", "South Dakota", "Wyoming"],
        # End state: DE, HI, LA, MS, NV, NY, RI, SC, SD, WY (10 states)
    },
}

# Expected end states for verification
EXPECTED_END_STATES = {
    "T1": sorted(["Illinois", "Kansas", "Massachusetts", "Minnesota",
                   "New Hampshire", "New Jersey", "New Mexico"]),
    "T2": sorted(["Arizona", "Arkansas", "Colorado", "Georgia", "Idaho",
                   "Kentucky", "Missouri", "North Carolina", "Ohio",
                   "Tennessee", "Utah"]),
    "T3": sorted(["Alabama", "Connecticut", "Indiana", "Iowa", "Maryland",
                   "Michigan", "Nebraska", "Oklahoma", "Oregon",
                   "Pennsylvania", "Virginia", "Washington", "Wisconsin",
                   "West Virginia"]),
    "T4": sorted(["Delaware", "Hawaii", "Louisiana", "Mississippi", "Nevada",
                   "New York", "Rhode Island", "South Carolina",
                   "South Dakota", "Wyoming"]),
}


def generate(output_dir, dry_run=False):
    os.makedirs(output_dir, exist_ok=True)
    date_stamp = datetime.now().strftime("%Y-%m-%d")

    # ============================================================
    # FILE 1: Location changes (import FIRST)
    # ============================================================

    loc_rows = []

    # T1, T2, T3: standard remove + add
    for tier in ["T1", "T2", "T3"]:
        campaign = CURRENT_NAMES[tier]
        changes = CHANGES[tier]

        for state in changes.get("remove", []):
            loc_rows.append({
                "Campaign": campaign,
                "ID": GEO_IDS[state],
                "Location": f"{state}, United States",
                "Status": "Removed",
            })

        for state in changes.get("add", []):
            loc_rows.append({
                "Campaign": campaign,
                "ID": GEO_IDS[state],
                "Location": f"{state}, United States",
                "Status": "Enabled",
            })

    # T4: strip stale + add new (keeps stay untouched)
    t4_campaign = CURRENT_NAMES["T4"]
    for state in CHANGES["T4"]["strip_stale"]:
        loc_rows.append({
            "Campaign": t4_campaign,
            "ID": GEO_IDS[state],
            "Location": f"{state}, United States",
            "Status": "Removed",
        })

    for state in CHANGES["T4"]["add"]:
        loc_rows.append({
            "Campaign": t4_campaign,
            "ID": GEO_IDS[state],
            "Location": f"{state}, United States",
            "Status": "Enabled",
        })

    # ============================================================
    # FILE 2: Campaign renames + T4 enable (import AFTER locations)
    # ============================================================

    campaign_rows = []

    for tier in ["T1", "T2", "T3", "T4"]:
        row = {
            "Campaign#Original": CURRENT_NAMES[tier],
            "Campaign": NEW_NAMES[tier],
        }
        if tier == "T4":
            row["Campaign Status"] = "Enabled"
        else:
            row["Campaign Status"] = ""
        campaign_rows.append(row)

    # ============================================================
    # Summary & verification
    # ============================================================

    # Count operations
    removes = [r for r in loc_rows if r["Status"] == "Removed"]
    adds = [r for r in loc_rows if r["Status"] == "Enabled"]

    print(f"\n{'='*70}")
    print("GOOGLE HOME RPL k=4 RE-TIER — BUILD SUMMARY")
    print(f"{'='*70}")
    print(f"\n  Location changes:")
    print(f"    Removes: {len(removes)}")
    print(f"    Adds:    {len(adds)}")
    print(f"    Total:   {len(loc_rows)}")

    for tier in ["T1", "T2", "T3", "T4"]:
        campaign = CURRENT_NAMES[tier]
        tier_removes = [r for r in removes if r["Campaign"] == campaign]
        tier_adds = [r for r in adds if r["Campaign"] == campaign]
        print(f"\n    {tier} ({campaign[:45]}...):")
        print(f"      Remove: {', '.join(sorted(r['Location'].split(',')[0] for r in tier_removes)) or '(none)'}")
        print(f"      Add:    {', '.join(sorted(r['Location'].split(',')[0] for r in tier_adds)) or '(none)'}")

    print(f"\n  Campaign changes:")
    for cr in campaign_rows:
        status_note = " + ENABLE" if cr.get("Campaign Status") == "Enabled" else ""
        print(f"    {cr['Campaign#Original'][:45]}...")
        print(f"    → {cr['Campaign']}{status_note}")

    # Verify end states
    print(f"\n  End-state verification (computed from current + changes):")
    current_states = {
        "T1": {"Alabama", "Delaware", "Minnesota", "Ohio", "Oregon", "Utah", "Wisconsin"},
        "T2": {"Arizona", "Arkansas", "Georgia", "Idaho", "Illinois", "Kansas", "Maryland",
               "Missouri", "New Hampshire", "New Jersey", "New Mexico", "North Carolina",
               "Oklahoma", "Pennsylvania", "Tennessee"},
        "T3": {"Colorado", "Connecticut", "Hawaii", "Indiana", "Iowa", "Kentucky", "Louisiana",
               "Massachusetts", "Michigan", "Mississippi", "Nebraska", "Nevada", "New York",
               "Rhode Island", "South Carolina", "South Dakota", "Virginia", "Washington",
               "West Virginia", "Wyoming"},
        "T4": {"Colorado", "Connecticut", "Hawaii", "Louisiana", "Nevada", "New York",
               "South Carolina", "Virginia"},
    }

    all_pass = True
    total_states = 0
    for tier in ["T1", "T2", "T3", "T4"]:
        cs = current_states[tier].copy()
        changes = CHANGES[tier]

        if tier == "T4":
            cs -= set(changes.get("strip_stale", []))
            cs |= set(changes.get("add", []))
        else:
            cs -= set(changes.get("remove", []))
            cs |= set(changes.get("add", []))

        expected = EXPECTED_END_STATES[tier]
        actual = sorted(cs)
        match = actual == expected
        all_pass = all_pass and match
        total_states += len(actual)
        print(f"    {tier}: {len(actual)} states — {'PASS' if match else 'FAIL'}")
        if not match:
            print(f"      Expected: {expected}")
            print(f"      Actual:   {actual}")

    print(f"    Total tiered states: {total_states} (expected 42): "
          f"{'PASS' if total_states == 42 else 'FAIL'}")

    # Check no dual-targeting
    all_states = []
    for tier in ["T1", "T2", "T3", "T4"]:
        cs = current_states[tier].copy()
        changes = CHANGES[tier]
        if tier == "T4":
            cs -= set(changes.get("strip_stale", []))
            cs |= set(changes.get("add", []))
        else:
            cs -= set(changes.get("remove", []))
            cs |= set(changes.get("add", []))
        all_states.extend(cs)

    dupes = [s for s in set(all_states) if all_states.count(s) > 1]
    print(f"    No dual-targeting: {'PASS' if not dupes else 'FAIL — ' + str(dupes)}")

    # Manual reminders
    print(f"\n{'='*70}")
    print("MANUAL STEPS (do in Editor BEFORE posting):")
    print("  F1: Detach T1 (23496334752) from portfolio 'Tier 2 - Combined'")
    print("      → campaign-level tROAS 115%")
    print("  F2: Detach T4 (23428389081) from portfolio 'Tier 1 - Combined'")
    print("      → campaign-level tROAS 112%")
    print("  T4: Set budget per plan workbook (~$130/33d-equivalent)")
    print("  T4: Audit inherited keywords/ads from legacy life (226 KW, 48 ads)")

    if dry_run:
        print(f"\n{'='*70}")
        print("DRY RUN — no files written")
        return

    # Write location file
    loc_path = os.path.join(output_dir, f"retier_locations_{date_stamp}.csv")
    with open(loc_path, "w", encoding="utf-16-le", newline="") as f:
        headers = ["Campaign", "ID", "Location", "Status"]
        writer = csv.DictWriter(f, fieldnames=headers, delimiter="\t")
        writer.writeheader()
        for row in loc_rows:
            writer.writerow(row)
    print(f"\n  Locations file: {loc_path}")

    # Write campaign file
    camp_path = os.path.join(output_dir, f"retier_campaign_{date_stamp}.csv")
    with open(camp_path, "w", encoding="utf-16-le", newline="") as f:
        headers = ["Campaign#Original", "Campaign", "Campaign Status"]
        writer = csv.DictWriter(f, fieldnames=headers, delimiter="\t")
        writer.writeheader()
        for row in campaign_rows:
            writer.writerow(row)
    print(f"  Campaign file:  {camp_path}")

    print(f"\n{'='*70}")
    print("IMPORT ORDER:")
    print("  1. Resolve F1 + F2 (portfolio detaches) manually in Editor")
    print("  2. Import retier_locations file")
    print("  3. Import retier_campaign file (renames + T4 enable)")
    print("  4. Review ALL staged changes before posting")
    print("  5. Post as ONE change set")
    print("  6. Export locations (incl. paused) for verification run")
    print("     → Ledger Comparison must read 100% SAME (42 tiered + 3 standalone)")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Google Home RPL k=4 Re-Tier Build")
    p.add_argument("--output-dir", default=OUTPUT_DIR)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    generate(a.output_dir, a.dry_run)
