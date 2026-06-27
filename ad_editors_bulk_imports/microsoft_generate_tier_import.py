"""
generate_tier_import.py — Microsoft Ads Bulk Import Generator for Geo-Tier Restructuring

Generates two CSV files per run:
  1. combined_tier{N}_import.csv       — campaign updates + add locations + delete old locations
  2. delete_locations_tier{N}.csv      — standalone delete file (old campaign locations)

Usage:
  python microsoft_generate_tier_import.py --tiers 3
  python microsoft_generate_tier_import.py --tiers 4,5
  python microsoft_generate_tier_import.py --tiers 3,4,5,6 --dry-run

Inputs:
  - TIER_CONFIG below (edit per quarter)
  - Location export CSV from Microsoft Ads Editor (UTF-16 LE, tab-delimited)

Output format: Microsoft Bulk API CSV (comma-separated, Format Version 6.0)
"""

import csv
import argparse
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ============================================================
# CONFIGURATION — edit this section each quarter
# ============================================================

# Path to the Microsoft Ads Editor location export (UTF-16 LE, tab-delimited)
LOCATION_EXPORT_PATH = r"C:\Projects\google-ads-ops\ad_editors_bulk_imports\input\Home Insurance_PositiveLocations_current_view_2026-06-25.csv"

# Output directory
OUTPUT_DIR = r"C:/Projects/google-ads-ops/ad_editors_bulk_imports/output"

# Microsoft state target codes (from location export — verified values)
STATE_TARGETS = {
    "Alabama": "4080",   "Alaska": "4079",    "Arizona": "4083",
    "Arkansas": "4081",  "California": "4084", "Colorado": "4085",
    "Connecticut": "4086", "Delaware": "4088", "Florida": "4089",
    "Georgia": "4090",   "Hawaii": "4092",    "Idaho": "4094",
    "Illinois": "4095",  "Indiana": "4096",   "Iowa": "4093",
    "Kansas": "4097",    "Kentucky": "4098",  "Louisiana": "4099",
    "Maine": "4102",     "Maryland": "4101",  "Massachusetts": "4100",
    "Michigan": "4103",  "Minnesota": "4104", "Mississippi": "4107",
    "Missouri": "4105",  "Montana": "4108",   "Nebraska": "4111",
    "Nevada": "4115",    "New Hampshire": "4112", "New Jersey": "4113",
    "New Mexico": "4114", "New York": "4116",  "North Carolina": "4109",
    "North Dakota": "4110", "Ohio": "4117",    "Oklahoma": "4118",
    "Oregon": "4119",    "Pennsylvania": "4120", "Rhode Island": "4122",
    "South Carolina": "4123", "South Dakota": "4124", "Tennessee": "4125",
    "Texas": "4126",     "Utah": "4128",      "Vermont": "4131",
    "Virginia": "4129",  "Washington": "4132", "West Virginia": "4134",
    "Wisconsin": "4133", "Wyoming": "4135",
}

# Campaign shell definitions — one entry per tier
# Only tiers being UPDATED need campaign_updates populated.
# If the shell campaign already has the correct name/tCPA/tracking, set campaign_updates to None.
TIER_CONFIG = {
    1: {
        "shell_id": "531494235",
        "campaign_updates": {
            "name": "HOME INSURANCE_STATE-GROUP_Tier1RPL80Plus_COMBINED_TCPA_SEARCH_ALLDEVICE_USA-STE",
            "tcpa": "39.01",
            "bid_strategy_type": "TargetCpa",
            "tracking_template": "{lpurl}?utm_source=bing&utm_medium=cpc&utm_content=Home&utm_campaign=STATEGROUP_TIER1RPL80Plus_COMBINED_TCPA_alldevice",
        },
        "states": [
            # (state, current_campaign_parent_id)
            # Colorado is the anchor — already in 531494235, skip for add but no delete needed
            ("Colorado",    "531494235"),  # anchor — stays
            ("Illinois",    "531494280"),
            ("New Jersey",  "531494253"),
            ("Oregon",      "531494281"),
            ("Delaware",    "531494253"),
            ("Vermont",     "531494274"),
            ("Iowa",        "531494273"),
            ("New Mexico",  "531494274"),
        ],
    },
    2: {
        "shell_id": "531494261",
        "campaign_updates": {
            "name": "HOME INSURANCE_STATE-GROUP_Tier2RPL65Plus_COMBINED_TCPA_SEARCH_ALLDEVICE_USA-STE",
            "tcpa": "35",
            "bid_strategy_type": "TargetCpa",
            "tracking_template": "{lpurl}?utm_source=bing&utm_medium=cpc&utm_content=Home&utm_campaign=STATEGROUP_TIER2RPL65Plus_COMBINED_TCPA_alldevice",
        },
        "states": [
            ("Texas",         "531494261"),  # anchor — stays
            ("Massachusetts", "531494274"),
            ("Minnesota",     "531494275"),
            ("Missouri",      "531494235"),
            ("Alaska",        "531494245"),
            ("Utah",          "531494275"),
            ("Kansas",        "531494280"),
        ],
    },
    3: {
        "shell_id": "531494273",
        "campaign_updates": {
            "name": "HOME INSURANCE_STATE-GROUP_Tier3RPL55Plus_COMBINED_TCPA_SEARCH_ALLDEVICE_USA-STE",
            "tcpa": "31.19",
            "bid_strategy_type": "TargetCpa",
            "tracking_template": "{lpurl}?utm_source=bing&utm_medium=cpc&utm_content=Home&utm_campaign=STATEGROUP_TIER3RPL55Plus_COMBINED_TCPA_alldevice",
        },
        "states": [
            ("Arizona",       "531494273"),  # anchor — stays
            ("Georgia",       "531494273"),  # anchor — stays
            ("Arkansas",      "531494275"),
            ("Maryland",      "531494274"),
            ("Ohio",          "531494280"),
            ("Oklahoma",      "531494280"),
            ("Tennessee",     "531494235"),
            ("Wisconsin",     "531494280"),
            ("Idaho",         "531494275"),
            ("New Hampshire", "531494281"),
            ("Connecticut",   "531494245"),
            ("Nebraska",      "531494280"),
            ("Maine",         "531494253"),
        ],
    },
    4: {
        "shell_id": "531494275",
        "campaign_updates": {
            "name": "HOME INSURANCE_STATE-GROUP_Tier4RPL45Plus_COMBINED_TCPA_SEARCH_ALLDEVICE_USA-STE",
            "tcpa": "25",
            "bid_strategy_type": "TargetCpa",
            "tracking_template": "{lpurl}?utm_source=bing&utm_medium=cpc&utm_content=Home&utm_campaign=STATEGROUP_TIER4RPL45Plus_COMBINED_TCPA_alldevice",
        },
        "states": [
            ("Pennsylvania",  "531494275"),  # anchor — stays
            ("Virginia",      "531494281"),
            ("Mississippi",   "531494274"),
            ("New York",      "531494260"),
            ("Louisiana",     "531494260"),
            ("Rhode Island",  "531494280"),
            ("South Carolina","531494245"),
            ("West Virginia", "531494245"),
            ("South Dakota",  "531494277"),
        ],
    },
    5: {
        "shell_id": "531494275",
        "campaign_updates": {
            "name": "HOME INSURANCE_STATE-GROUP_Tier5RPL35Plus_COMBINED_TCPA_SEARCH_ALLDEVICE_USA-STE",
            "tcpa": "29",
            "bid_strategy_type": "TargetCpa",
            "tracking_template": "{lpurl}?utm_source=bing&utm_medium=cpc&utm_content=Home&utm_campaign=STATEGROUP_TIER5RPL35Plus_COMBINED_TCPA_alldevice",
        },
        "states": [
            # Anchors (already in 531494275)
            ("Indiana",        "531494275"),  # anchor — stays
            ("Pennsylvania",   "531494275"),  # anchor — stays
            # From KENTUCKY campaign (531494277) — campaign empties after this, pause it
            ("Kentucky",       "531494277"),
            ("Washington",     "531494277"),
            ("South Dakota",   "531494277"),
            # From VIRGINIA campaign (531494281)
            ("Alabama",        "531494281"),
            ("Michigan",       "531494281"),
            ("Montana",        "531494281"),
            ("Virginia",       "531494281"),  # absorbed from retired Tier 4
            # From NORTH CAROLINA campaign (531494245) — campaign empties after this, pause it
            ("North Carolina", "531494245"),
            ("Nevada",         "531494245"),
            ("South Carolina", "531494245"),  # below-threshold
            ("West Virginia",  "531494245"),  # below-threshold
            ("North Dakota",   "531494245"),  # below-threshold, unclassified → Tier 5
            # From FLORIDA campaign (531494260) — HI & LA stay in Florida
            ("New York",       "531494260"),  # absorbed from retired Tier 4
            # From MASSACHUSETTS campaign (531494274)
            ("Mississippi",    "531494274"),  # below-threshold
            # From ILLINOIS campaign (531494280)
            ("Rhode Island",   "531494280"),  # below-threshold
        ],
        # Source campaigns that fully empty after this migration.
        # Instead of deleting individual locations, pause the entire campaign.
        # This preserves location criteria and bidder history for rollback.
        "pause_campaigns": [
            "531494277",  # KENTUCKY — KY, WA, SD all leaving
            "531494281",  # VIRGINIA — AL, MI, MT, VA all leaving (OR, NH moved in T1-3)
            "531494274",  # MASSACHUSETTS — MS leaving (MA, VT, NM, MD moved in T1-3)
            "531494280",  # ILLINOIS — RI leaving (IL, KS, OH, OK, WI, NE moved in T1-3)
            "531494245",  # NORTH CAROLINA — NC, NV, SC, WV, ND all leaving (AK, CT moved in T1-3)
        ],
    },
    6: {
        "shell_id": "531494232",
        "campaign_updates": {
            "name": "HOME INSURANCE_STATE-GROUP_Tier6RPL20Plus_COMBINED_TCPA_SEARCH_ALLDEVICE_USA-STE",
            "tcpa": "14",
            "bid_strategy_type": "TargetCpa",
            "tracking_template": "{lpurl}?utm_source=bing&utm_medium=cpc&utm_content=Home&utm_campaign=STATEGROUP_TIER6RPL20Plus_COMBINED_TCPA_alldevice",
        },
        "states": [
            ("California",    "531494232"),  # anchor — stays
            ("Florida",       "531494260"),
        ],
    },
}


# ============================================================
# BULK FORMAT CONSTANTS
# ============================================================

BULK_HEADERS = [
    "Type", "Status", "Id", "Parent Id", "Sub Type", "Campaign",
    "Client Id", "Modified Time", "Budget Id", "Budget Name", "Budget",
    "Budget Type", "Bid Adjustment", "Name", "Country Code", "Store Id",
    "Campaign Type", "Language", "Target Setting", "Priority",
    "Tracking Template", "Final Url Suffix", "Custom Parameter",
    "Bid Strategy Type", "Domain Language", "Source", "Is political",
    "Bid Strategy TargetCpa", "Target",
    "Radius", "Unit", "From Hour", "From Minute", "To Hour", "To Minute",
    "Latitude", "Longitude",
]


def empty_row() -> Dict[str, str]:
    return {h: "" for h in BULK_HEADERS}


def format_version_row() -> Dict[str, str]:
    row = empty_row()
    row["Type"] = "Format Version"
    row["Name"] = "6.0"
    return row


# ============================================================
# TARGET CODE LOOKUP
# ============================================================

def load_target_codes_from_export(path: str) -> Dict[str, str]:
    """Read the Microsoft Ads Editor location export and build state → target code map.
    Falls back to STATE_TARGETS if file not found."""
    if not os.path.exists(path):
        print(f"  Location export not found at {path}, using built-in STATE_TARGETS")
        return STATE_TARGETS

    lookup: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-16-le") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                name = row.get("Name", "").strip()
                target = row.get("Target", "").strip()
                if name and target:
                    state = name.split(",")[0].strip()
                    if state not in lookup:
                        lookup[state] = target
        print(f"  Loaded {len(lookup)} target codes from {path}")
    except Exception as e:
        print(f"  Error reading location export: {e}. Using built-in STATE_TARGETS")
        return STATE_TARGETS

    return lookup


def load_location_ids_from_export(path: str) -> Dict[Tuple[str, str], str]:
    """Build (state, parent_id) → location_criterion_id map for delete operations."""
    lookup: Dict[Tuple[str, str], str] = {}
    if not os.path.exists(path):
        return lookup

    try:
        with open(path, "r", encoding="utf-16-le") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                name = row.get("Name", "").strip()
                loc_id = row.get("ID", "").strip().strip("[]")
                parent_id = row.get("Parent Id", "").strip().strip("[]")
                if name and loc_id and parent_id:
                    state = name.split(",")[0].strip()
                    lookup[(state, parent_id)] = loc_id
    except Exception as e:
        print(f"  Error reading location IDs: {e}")

    return lookup


# ============================================================
# ROW GENERATORS
# ============================================================

def make_campaign_row(shell_id: str, updates: Dict[str, str]) -> Dict[str, str]:
    row = empty_row()
    row["Type"] = "Campaign"
    row["Status"] = "Active"
    row["Id"] = shell_id
    row["Campaign"] = updates["name"]
    row["Bid Strategy Type"] = updates["bid_strategy_type"]
    row["Bid Strategy TargetCpa"] = updates["tcpa"]
    row["Tracking Template"] = updates["tracking_template"]
    return row


def make_add_location_row(shell_id: str, target_code: str) -> Dict[str, str]:
    row = empty_row()
    row["Type"] = "Campaign Location Criterion"
    row["Status"] = "Active"
    row["Parent Id"] = shell_id
    row["Sub Type"] = "State"
    row["Target"] = target_code
    row["Bid Adjustment"] = "0"
    return row


def make_delete_location_row(loc_id: str, parent_id: str, target_code: str) -> Dict[str, str]:
    row = empty_row()
    row["Type"] = "Campaign Location Criterion"
    row["Status"] = "Deleted"
    row["Id"] = loc_id
    row["Parent Id"] = parent_id
    row["Target"] = target_code
    return row


def make_pause_campaign_row(campaign_id: str) -> Dict[str, str]:
    """Pause an entire campaign instead of deleting individual locations."""
    row = empty_row()
    row["Type"] = "Campaign"
    row["Status"] = "Paused"
    row["Id"] = campaign_id
    return row


# ============================================================
# MAIN GENERATOR
# ============================================================

def generate_tier_files(
    tiers: List[int],
    dry_run: bool = False,
    location_export: Optional[str] = None,
    output_dir: Optional[str] = None,
):
    export_path = location_export or LOCATION_EXPORT_PATH
    out_dir = output_dir or OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    print(f"Loading target codes from location export ...")
    target_codes = load_target_codes_from_export(export_path)
    location_ids = load_location_ids_from_export(export_path)

    tier_label = "_".join(str(t) for t in tiers)
    date_stamp = datetime.now().strftime("%Y-%m-%d")

    combined_rows: List[Dict[str, str]] = [format_version_row()]
    delete_rows: List[Dict[str, str]] = [format_version_row()]

    summary_lines: List[str] = []

    for tier_num in tiers:
        cfg = TIER_CONFIG.get(tier_num)
        if cfg is None:
            print(f"  WARNING: Tier {tier_num} not found in TIER_CONFIG, skipping")
            continue

        shell_id = cfg["shell_id"]
        updates = cfg.get("campaign_updates")
        states = cfg["states"]

        print(f"\n--- Tier {tier_num} (shell {shell_id}) ---")

        # Section 1: Campaign update
        if updates:
            combined_rows.append(make_campaign_row(shell_id, updates))
            print(f"  Campaign update: {updates['name']}")
            print(f"    tCPA: ${updates['tcpa']}")
        else:
            print(f"  Campaign update: skipped (no changes)")

        # Section 2: Add locations
        add_count = 0
        skip_count = 0
        for state, current_parent_id in states:
            tc = target_codes.get(state)
            if not tc:
                print(f"  WARNING: No target code for '{state}', skipping")
                continue

            is_anchor = (current_parent_id == shell_id)
            if is_anchor:
                skip_count += 1
                continue

            combined_rows.append(make_add_location_row(shell_id, tc))
            add_count += 1

        print(f"  Add locations: {add_count} new, {skip_count} anchors (already in shell)")

        # Section 3: Delete old locations OR pause entire source campaigns
        pause_campaigns = set(cfg.get("pause_campaigns", []))
        paused_campaign_ids = set()
        del_count = 0
        pause_count = 0

        for state, current_parent_id in states:
            is_anchor = (current_parent_id == shell_id)
            if is_anchor:
                continue

            # If source campaign is being paused, skip individual deletes
            if current_parent_id in pause_campaigns:
                if current_parent_id not in paused_campaign_ids:
                    pause_row = make_pause_campaign_row(current_parent_id)
                    combined_rows.append(pause_row)
                    paused_campaign_ids.add(current_parent_id)
                    pause_count += 1
                continue

            tc = target_codes.get(state)
            if not tc:
                continue

            loc_key = (state, current_parent_id)
            loc_id = location_ids.get(loc_key, "")

            if loc_id:
                del_row = make_delete_location_row(loc_id, current_parent_id, tc)
                combined_rows.append(del_row)
                delete_rows.append(del_row)
                del_count += 1
            else:
                print(f"  WARNING: No location criterion ID for {state} in campaign {current_parent_id}")
                print(f"    → Delete row omitted. Remove manually in Editor if needed.")

        print(f"  Delete old locations: {del_count}")
        if pause_count:
            print(f"  Pause source campaigns: {pause_count} ({', '.join(sorted(paused_campaign_ids))})")

        summary_lines.append(
            f"Tier {tier_num}: {add_count} adds, {del_count} deletes, "
            f"{pause_count} campaign pauses, "
            f"{skip_count} anchors, shell={shell_id}"
        )

    # Write files
    combined_path = os.path.join(out_dir, f"combined_tier{tier_label}_import_{date_stamp}.csv")
    delete_path = os.path.join(out_dir, f"delete_locations_tier{tier_label}_{date_stamp}.csv")

    if dry_run:
        print(f"\n{'='*60}")
        print("DRY RUN — no files written")
        print(f"  Combined rows: {len(combined_rows) - 1}")
        print(f"  Delete rows:   {len(delete_rows) - 1}")
        for line in summary_lines:
            print(f"  {line}")
        return

    with open(combined_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=BULK_HEADERS)
        writer.writeheader()
        for row in combined_rows:
            writer.writerow(row)

    with open(delete_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=BULK_HEADERS)
        writer.writeheader()
        for row in delete_rows:
            writer.writerow(row)

    print(f"\n{'='*60}")
    print("FILES WRITTEN")
    print(f"  Combined: {combined_path}  ({len(combined_rows)-1} rows)")
    print(f"  Deletes:  {delete_path}  ({len(delete_rows)-1} rows)")
    for line in summary_lines:
        print(f"  {line}")


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate Microsoft Ads Bulk import files for geo-tier restructuring"
    )
    parser.add_argument(
        "--tiers", required=True,
        help="Comma-separated tier numbers to generate (e.g., 3 or 4,5)"
    )
    parser.add_argument(
        "--location-export", default=LOCATION_EXPORT_PATH,
        help="Path to Microsoft Ads Editor location export CSV"
    )
    parser.add_argument(
        "--output-dir", default=OUTPUT_DIR,
        help="Output directory for generated files"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would be generated without writing files"
    )

    args = parser.parse_args()
    tier_list = [int(t.strip()) for t in args.tiers.split(",")]

    generate_tier_files(
        tiers=tier_list,
        dry_run=args.dry_run,
        location_export=args.location_export,
        output_dir=args.output_dir,
    )
