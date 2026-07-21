"""
google_generate_tier_import.py — Google Ads Editor Import Generator for Geo-Tier Restructuring

Generates two CSV files per rollout week:
  1. locations_tier{N}_{date}.csv    — add/remove state locations (import FIRST)
  2. campaign_tier{N}_{date}.csv     — campaign rename + tracking template (import SECOND)

Usage:
  python C:\Projects\google-ads-ops\ad_editors_bulk_imports\google_generate_tier_import.py --week 1
  python google_generate_tier_import.py --week 2 --dry-run
  python google_generate_tier_import.py --week 1,2,3,4

Format: Google Ads Editor CSV (UTF-16 LE, tab-delimited)
Import order: locations first, then campaign settings (locations reference current names).

Rollout schedule (best-tier-first):
  Week 1 (6/16): TIER ONE  → Tier Two shell
  Week 2 (6/23): TIER TWO  → Tier Three shell
  Week 3 (6/30): TIER THREE → Tier One shell
  Week 4 (7/7):  TIER FOUR  → Tier Four shell
"""

import csv
import argparse
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ============================================================
# CONFIGURATION
# ============================================================

OUTPUT_DIR = r"C:/Projects/google-ads-ops/ad_editors_bulk_imports/output"

# Original campaign names (before any renames)
SHELL_ORIGINAL_NAMES = {
    "Tier One":   "HOME INSURANCE-TIER ONE-NB-SEARCH-ALL-USA-STE",
    "Tier Two":   "HOME INSURANCE-TIER TWO-NB-SEARCH-ALL-USA-STE",
    "Tier Three": "HOME INSURANCE-TIER THREE-NB-SEARCH-ALL-USA-STE",
    "Tier Four":  "HOME INSURANCE-TIER FOUR-NB-SEARCH-ALL-USA-STE",
}

# Campaign IDs (for reference / future API use)
SHELL_CAMPAIGN_IDS = {
    "Tier One":   "23428389081",
    "Tier Two":   "23496334752",
    "Tier Three": "23530860942",
    "Tier Four":  "23586637678",
}

# Rollout config — ordered by week. Each week:
#   destination_shell: which physical shell campaign receives the new tier
#   new_name: what the shell gets renamed to AFTER locations are moved
#   tracking_template: new tracking template for the shell
#   states: list of (state_name, current_shell) tuples
#     - if current_shell == destination_shell, the state is an "anchor" (no move needed)
#     - otherwise, state is added to destination and removed from current
WEEK_CONFIG = {
    1: {
        "new_tier_label": "TIER ONE",
        "rollout_date": "2026-06-16",
        "destination_shell": "Tier Two",
        "new_name": "HOME INSURANCE_STATE-GROUP_Tier1RPL80Plus_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
        "tracking_template": "{lpurl}?utm_source=google&utm_medium=cpc&utm_content=Home&utm_campaign=STATEGROUP_TIER1RPL80Plus_COMBINED_TROAS_alldevice",
        "states": [
            ("Utah",      "Tier Two"),    # anchor
            ("Ohio",      "Tier Three"),
            ("Alabama",   "Tier Four"),
            ("Minnesota", "Tier Three"),
            ("Wisconsin", "Tier Four"),
            ("Oregon",    "Tier Four"),
            ("Delaware",  "Tier Three"),  # below-threshold, nearest TIER ONE
        ],
    },
    2: {
        "new_tier_label": "TIER TWO",
        "rollout_date": "2026-06-23",
        "destination_shell": "Tier Three",
        "new_name": "HOME INSURANCE_STATE-GROUP_Tier2RPL70Plus_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
        "tracking_template": "{lpurl}?utm_source=google&utm_medium=cpc&utm_content=Home&utm_campaign=STATEGROUP_TIER2RPL70Plus_COMBINED_TROAS_alldevice",
        "states": [
            ("Georgia",        "Tier Three"),  # anchor
            ("Arkansas",       "Tier Three"),  # anchor
            ("Oklahoma",       "Tier Three"),  # anchor
            ("Tennessee",      "Tier Three"),  # anchor
            ("Idaho",          "Tier Three"),  # anchor
            ("New Jersey",     "Tier One"),
            ("North Carolina", "Tier One"),
            ("Illinois",       "Tier Four"),
            ("Pennsylvania",   "Tier Four"),
            ("Maryland",       "Tier Four"),
            ("Kentucky",       "Tier Four"),
        ],
    },
    3: {
        "new_tier_label": "TIER THREE",
        "rollout_date": "2026-06-30",
        "destination_shell": "Tier One",
        "new_name": "HOME INSURANCE_STATE-GROUP_Tier3RPL60Plus_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
        "tracking_template": "{lpurl}?utm_source=google&utm_medium=cpc&utm_content=Home&utm_campaign=STATEGROUP_TIER3RPL60Plus_COMBINED_TROAS_alldevice",
        "states": [
            ("Connecticut",    "Tier One"),    # anchor
            ("South Carolina", "Tier One"),    # anchor
            ("Nevada",         "Tier One"),    # anchor
            ("Louisiana",      "Tier One"),    # anchor
            ("Colorado",       "Tier Two"),
            ("Arizona",        "Tier Three"),
            ("Missouri",       "Tier Three"),
            ("Kansas",         "Tier Three"),
            ("Massachusetts",  "Tier Four"),
            ("Iowa",           "Tier Four"),
            ("Rhode Island",   "Tier Four"),
            ("West Virginia",  "Tier Four"),   # below-threshold, nearest TIER THREE
        ],
    },
    4: {
        "new_tier_label": "TIER FOUR",
        "rollout_date": "2026-07-07",
        "destination_shell": "Tier Four",
        "new_name": "HOME INSURANCE_STATE-GROUP_Tier4RPL40Plus_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
        "tracking_template": "{lpurl}?utm_source=google&utm_medium=cpc&utm_content=Home&utm_campaign=STATEGROUP_TIER4RPL40Plus_COMBINED_TROAS_alldevice",
        "states": [
            ("Michigan",       "Tier Four"),   # anchor
            ("Washington",     "Tier Four"),   # anchor
            ("Mississippi",    "Tier Four"),   # anchor
            ("New Mexico",     "Tier Four"),   # anchor
            ("South Dakota",   "Tier Four"),   # anchor (below-threshold, nearest TIER FOUR)
            ("Wyoming",        "Tier Four"),   # anchor (below-threshold, nearest TIER FOUR)
            ("Virginia",       "Tier One"),
            ("New York",       "Tier One"),
            ("Hawaii",         "Tier One"),
            ("Indiana",        "Tier Three"),
            ("Nebraska",       "Tier Three"),
            ("New Hampshire",  "Tier Three"),  # below-threshold, nearest TIER FOUR
        ],
    },
}

# Rename sequence: which shell gets renamed in which week
# Used to track current campaign names across weeks
RENAME_SEQUENCE = {
    1: "Tier Two",
    2: "Tier Three",
    3: "Tier One",
    4: "Tier Four",
}


# ============================================================
# NAME TRACKER
# ============================================================

def get_current_shell_names(up_to_week: int) -> Dict[str, str]:
    """Return the current campaign name for each shell, accounting for
    renames that happened in previous weeks."""
    names = dict(SHELL_ORIGINAL_NAMES)
    for w in range(1, up_to_week):
        cfg = WEEK_CONFIG.get(w)
        if cfg:
            shell = RENAME_SEQUENCE[w]
            names[shell] = cfg["new_name"]
    return names


# ============================================================
# FILE GENERATORS
# ============================================================

def generate_location_file(
    week: int,
    shell_names: Dict[str, str],
    out_dir: str,
    dry_run: bool = False,
) -> Tuple[int, int, int]:
    """Generate location add/remove CSV for a given week.
    Returns (adds, removes, anchors)."""

    cfg = WEEK_CONFIG[week]
    dest_shell = cfg["destination_shell"]
    dest_campaign = shell_names[dest_shell]

    rows = []
    add_count = 0
    remove_count = 0
    anchor_count = 0

    for state, current_shell in cfg["states"]:
        location_name = f"{state}, United States"
        is_anchor = (current_shell == dest_shell)

        if is_anchor:
            anchor_count += 1
            continue

        current_campaign = shell_names[current_shell]

        # Add to destination
        rows.append({
            "Campaign": dest_campaign,
            "Location": location_name,
            "Status": "Enabled",
        })
        add_count += 1

        # Remove from source
        rows.append({
            "Campaign": current_campaign,
            "Location": location_name,
            "Status": "Removed",
        })
        remove_count += 1

    # Write file
    date_stamp = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(out_dir, f"locations_week{week}_tier{week}_{date_stamp}.csv")

    if not dry_run:
        with open(path, "w", encoding="utf-16-le", newline="") as f:
            headers = ["Campaign", "Location", "Status"]
            writer = csv.DictWriter(f, fieldnames=headers, delimiter="\t")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    print(f"  Locations: {add_count} adds, {remove_count} removes, {anchor_count} anchors")
    if not dry_run:
        print(f"  File: {path}")

    return add_count, remove_count, anchor_count


def generate_campaign_file(
    week: int,
    shell_names: Dict[str, str],
    out_dir: str,
    dry_run: bool = False,
):
    """Generate campaign rename + tracking template CSV for a given week."""

    cfg = WEEK_CONFIG[week]
    shell = RENAME_SEQUENCE[week]
    current_name = shell_names[shell]
    new_name = cfg["new_name"]
    tracking = cfg["tracking_template"]

    rows = [{
        "Campaign#Original": current_name,
        "Campaign": new_name,
        "Tracking Template": tracking,
    }]

    date_stamp = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(out_dir, f"campaign_week{week}_tier{week}_{date_stamp}.csv")

    if not dry_run:
        with open(path, "w", encoding="utf-16-le", newline="") as f:
            headers = ["Campaign#Original", "Campaign", "Tracking Template"]
            writer = csv.DictWriter(f, fieldnames=headers, delimiter="\t")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    print(f"  Campaign rename: {current_name}")
    print(f"               →  {new_name}")
    print(f"  Tracking: ...utm_campaign={tracking.split('utm_campaign=')[1]}")
    if not dry_run:
        print(f"  File: {path}")


# ============================================================
# MAIN
# ============================================================

def generate_week_files(
    weeks: List[int],
    dry_run: bool = False,
    output_dir: Optional[str] = None,
):
    out_dir = output_dir or OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    for week in weeks:
        cfg = WEEK_CONFIG.get(week)
        if cfg is None:
            print(f"WARNING: Week {week} not found in WEEK_CONFIG, skipping")
            continue

        # Get current shell names accounting for prior renames
        shell_names = get_current_shell_names(up_to_week=week)

        print(f"\n{'='*70}")
        print(f"WEEK {week} ({cfg['rollout_date']}) — {cfg['new_tier_label']}")
        print(f"  Destination shell: {cfg['destination_shell']} "
              f"({shell_names[cfg['destination_shell']]})")
        print(f"{'='*70}")

        # File 1: Locations (import FIRST)
        print("\n[1] Location changes:")
        generate_location_file(week, shell_names, out_dir, dry_run)

        # File 2: Campaign settings (import SECOND)
        print("\n[2] Campaign settings:")
        generate_campaign_file(week, shell_names, out_dir, dry_run)

    if dry_run:
        print(f"\n{'='*70}")
        print("DRY RUN — no files written")

    print(f"\n{'='*70}")
    print("IMPORT ORDER REMINDER:")
    print("  1. Import locations file FIRST (adds/removes use current campaign names)")
    print("  2. Import campaign file SECOND (renames the shell)")
    print("  3. Review all proposed changes in Editor before posting")


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate Google Ads Editor import files for geo-tier restructuring"
    )
    parser.add_argument(
        "--week", required=True,
        help="Comma-separated week numbers to generate (e.g., 1 or 1,2,3,4)"
    )
    parser.add_argument(
        "--output-dir", default=OUTPUT_DIR,
        help="Output directory for generated files"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview without writing files"
    )

    args = parser.parse_args()
    week_list = [int(w.strip()) for w in args.week.split(",")]

    generate_week_files(
        weeks=week_list,
        dry_run=args.dry_run,
        output_dir=args.output_dir,
    )
