"""
google_generate_tier_import.py — Google Ads Editor Import Generator for Geo-Tier Restructuring

Generates two CSV files per rollout week:
  1. locations_tier{N}_{date}.csv    — add/remove state locations (import FIRST)
  2. campaign_tier{N}_{date}.csv     — campaign rename + tracking template (import SECOND)

Usage:
  python C:/Projects/google-ads-ops/ad_editors_bulk_imports/google_generate_tier_import_v2.py --week 1
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

# Google Ads geo criterion IDs for US states
STATE_GEO_IDS = {
    "Alabama": 21133, "Alaska": 21132, "Arizona": 21136, "Arkansas": 21135,
    "California": 21137, "Colorado": 21138, "Connecticut": 21139, "Delaware": 21141,
    "District of Columbia": 21140, "Florida": 21142, "Georgia": 21143, "Hawaii": 21144,
    "Idaho": 21146, "Illinois": 21147, "Indiana": 21148, "Iowa": 21145,
    "Kansas": 21149, "Kentucky": 21150, "Louisiana": 21151, "Maine": 21154,
    "Maryland": 21153, "Massachusetts": 21152, "Michigan": 21155, "Minnesota": 21156,
    "Mississippi": 21158, "Missouri": 21157, "Montana": 21159, "Nebraska": 21162,
    "Nevada": 21166, "New Hampshire": 21163, "New Jersey": 21164, "New Mexico": 21165,
    "New York": 21167, "North Carolina": 21160, "North Dakota": 21161, "Ohio": 21168,
    "Oklahoma": 21169, "Oregon": 21170, "Pennsylvania": 21171, "Rhode Island": 21172,
    "South Carolina": 21173, "South Dakota": 21174, "Tennessee": 21175, "Texas": 21176,
    "Utah": 21177, "Vermont": 21179, "Virginia": 21178, "Washington": 21180,
    "West Virginia": 21183, "Wisconsin": 21182, "Wyoming": 21184,
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
        "rollout_date": "2026-06-30",
        "destination_shell": "Tier Three",
        "new_name": "HOME INSURANCE_STATE-GROUP_Tier2RPL70Plus_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
        "tracking_template": "{lpurl}?utm_source=google&utm_medium=cpc&utm_content=Home&utm_campaign=STATEGROUP_TIER2RPL70Plus_COMBINED_TROAS_alldevice",
        # Validated 6/29 model + cross-window stability. current_shell = VERIFY against live export.
        # Dropped from original config: Tennessee (flipped negative), Oklahoma/Kentucky/Idaho
        # (drifting toward THREE), Arkansas (volatile on thin volume). Held for next-window re-confirm.
        # Kansas and Missouri: Tier Three incumbents staying in place (no row needed, intentional).
        "states": [
            ("Arizona",        "Tier Three"),  # anchor — confirmed in Tier Three shell, stays
            ("New Jersey",     "Tier One"),     # confirmed: HOME INSURANCE-TIER ONE
            ("North Carolina", "Tier One"),     # confirmed: HOME INSURANCE-TIER ONE
            ("Pennsylvania",   "Tier Four"),    # confirmed: HOME INSURANCE-TIER FOUR
            ("Maryland",       "Tier Four"),    # confirmed: HOME INSURANCE-TIER FOUR
            ("Illinois",       "Tier Four"),    # confirmed: HOME INSURANCE-TIER FOUR
            ("Georgia",        "Tier Three"),   # confirmed: anchor — already in Tier Three shell
        ],
        # Redistribution + cross-shell adds the main loop can't express:
        #   (state, from_shell, to_shell) → add to to_shell, remove from from_shell
        "extra_location_moves": [
            ("Indiana",  "Tier Three", "Tier Four"),  # -$4.5/lead, strip before relabel
            ("Nebraska", "Tier Three", "Tier Four"),  # -$7.3/lead, strip before relabel
            ("Ohio",     "Tier Three", "Tier Two"),   # confirmed: Tier Three shell; "Tier Two" = renamed TIER ONE shell
        ],
    },
    3: {
        "new_tier_label": "TIER THREE (MERGED)",
        "rollout_date": "2026-07-08",
        "destination_shell": "Tier Four",
        "new_name": "HOME INSURANCE_STATE-GROUP_Tier3RPL50Plus_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
        "tracking_template": "{lpurl}?utm_source=google&utm_medium=cpc&utm_content=Home&utm_campaign=STATEGROUP_TIER3RPL50Plus_COMBINED_TROAS_alldevice",
        # Verified against live location export 7/1-7/7 (post-6/30 moves, no dual-serving).
        # Only states being MOVED — anchors already in Tier Four shell are not listed.
        # Source: campaign_id 23428389081 (Tier One shell) — will be emptied + paused.
        "states": [
            ("Colorado",       "Tier One"),
            ("Connecticut",    "Tier One"),
            ("Hawaii",         "Tier One"),
            ("Louisiana",      "Tier One"),
            ("Nevada",         "Tier One"),
            ("New York",       "Tier One"),
            ("South Carolina", "Tier One"),
            ("Virginia",       "Tier One"),
        ],
        # New Mexico promoted from Tier Four to Tier2RPL70Plus (campaign 23530860942)
        "extra_location_moves": [
            ("New Mexico", "Tier Four", "Tier Three"),  # "Tier Three" resolves to renamed Tier2RPL70Plus
        ],
        # Source campaign to pause + defensively rename after locations are removed.
        # CRITICAL: never let source sit enabled and empty (reverts to default geo targeting).
        "pause_source": {
            "shell": "Tier One",
            "retired_name": "zz-RETIRED-HOME INSURANCE-TIER ONE-LEGACY-NB-SEARCH-ALL-USA-STE",
        },
    },
}

# Rename sequence: which shell gets renamed in which week
# Used to track current campaign names across weeks
RENAME_SEQUENCE = {
    1: "Tier Two",
    2: "Tier Three",
    3: "Tier Four",
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
        geo_id = STATE_GEO_IDS.get(state, "")

        # Add to destination
        rows.append({
            "Campaign": dest_campaign,
            "ID": geo_id,
            "Location": location_name,
            "Status": "Enabled",
        })
        add_count += 1

        # Remove from source
        rows.append({
            "Campaign": current_campaign,
            "ID": geo_id,
            "Location": location_name,
            "Status": "Removed",
        })
        remove_count += 1

    # Write file
    date_stamp = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(out_dir, f"locations_week{week}_tier{week}_{date_stamp}.csv")

    if not dry_run:
        with open(path, "w", encoding="utf-16-le", newline="") as f:
            headers = ["Campaign", "ID", "Location", "Status"]
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
    """Generate campaign pause + rename CSV for a given week.
    Handles: destination shell rename, and optional source shell pause + defensive rename."""

    cfg = WEEK_CONFIG[week]
    shell = RENAME_SEQUENCE[week]
    current_name = shell_names[shell]
    new_name = cfg["new_name"]
    tracking = cfg["tracking_template"]

    rows = []

    # Pause source campaign (if configured) — BEFORE rename, same batch as location removals
    pause_cfg = cfg.get("pause_source")
    if pause_cfg:
        source_shell = pause_cfg["shell"]
        source_name = shell_names[source_shell]
        retired_name = pause_cfg["retired_name"]
        # Pause row
        rows.append({
            "Campaign#Original": source_name,
            "Campaign": retired_name,
            "Campaign Status": "Paused",
            "Tracking Template": "",
        })
        print(f"  Pause + retire: {source_name}")
        print(f"              →  {retired_name} (Paused)")

    # Rename destination shell
    rows.append({
        "Campaign#Original": current_name,
        "Campaign": new_name,
        "Campaign Status": "",
        "Tracking Template": tracking,
    })

    date_stamp = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(out_dir, f"campaign_week{week}_tier{week}_{date_stamp}.csv")

    if not dry_run:
        with open(path, "w", encoding="utf-16-le", newline="") as f:
            headers = ["Campaign#Original", "Campaign", "Campaign Status", "Tracking Template"]
            writer = csv.DictWriter(f, fieldnames=headers, delimiter="\t")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    print(f"  Campaign rename: {current_name}")
    print(f"               →  {new_name}")
    print(f"  Tracking: ...utm_campaign={tracking.split('utm_campaign=')[1]}")
    if not dry_run:
        print(f"  File: {path}")


def generate_extra_moves_file(
    week: int,
    shell_names: Dict[str, str],
    out_dir: str,
    dry_run: bool = False,
) -> int:
    """Generate add/remove pairs for cross-shell redistributions that the main
    loop can't express (e.g., stripping negative states before a relabel,
    or adding a state to a different destination shell)."""

    moves = WEEK_CONFIG[week].get("extra_location_moves", [])
    if not moves:
        return 0

    rows = []
    for state, from_shell, to_shell in moves:
        gid = STATE_GEO_IDS.get(state, "")
        loc = f"{state}, United States"
        rows.append({"Campaign": shell_names[to_shell],   "ID": gid, "Location": loc, "Status": "Enabled"})
        rows.append({"Campaign": shell_names[from_shell], "ID": gid, "Location": loc, "Status": "Removed"})

    date_stamp = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(out_dir, f"locations_week{week}_extra_{date_stamp}.csv")

    if not dry_run:
        with open(path, "w", encoding="utf-16-le", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["Campaign", "ID", "Location", "Status"], delimiter="\t")
            w.writeheader()
            w.writerows(rows)

    print(f"  Extra moves: {len(moves)} state(s) redistributed")
    if dry_run:
        for state, from_shell, to_shell in moves:
            print(f"    {state}: {from_shell} ({shell_names[from_shell][:40]}...)")
            print(f"         → {to_shell} ({shell_names[to_shell][:40]}...)")
    elif path:
        print(f"  File: {path}")

    return len(moves)


def check_verify_placeholders(week: int) -> List[str]:
    """Check for unresolved VERIFY placeholders in the config. Returns list of errors."""
    errors = []
    cfg = WEEK_CONFIG[week]

    for state, current_shell in cfg.get("states", []):
        if current_shell == "VERIFY":
            errors.append(f"  states: ({state!r}, 'VERIFY') — fill current_shell from live Editor export")

    for entry in cfg.get("extra_location_moves", []):
        state, from_shell, to_shell = entry
        if from_shell == "VERIFY":
            errors.append(f"  extra_location_moves: ({state!r}, 'VERIFY', ...) — fill from_shell")
        if to_shell == "VERIFY":
            errors.append(f"  extra_location_moves: ({state!r}, ..., 'VERIFY') — fill to_shell")

    return errors


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

        # Check for unresolved VERIFY placeholders
        verify_errors = check_verify_placeholders(week)
        if verify_errors:
            print(f"\n{'='*70}")
            print(f"ERROR: Week {week} has unresolved VERIFY placeholders.")
            print(f"Fill these from your live Google Ads Editor location export:")
            for err in verify_errors:
                print(err)
            print(f"{'='*70}")
            print("Aborting. No files written.")
            return

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

        # Print state-level from → to detail
        if dry_run:
            dest_shell = cfg["destination_shell"]
            dest_name = shell_names[dest_shell]
            for state, current_shell in cfg["states"]:
                if current_shell == dest_shell:
                    print(f"    {state}: anchor (stays in {dest_shell})")
                else:
                    print(f"    {state}: {current_shell} → {dest_shell}")
                    print(f"      add to:      {dest_name[:50]}...")
                    print(f"      remove from: {shell_names[current_shell][:50]}...")

        # File 1b: Extra location moves (cross-shell redistributions)
        extra = cfg.get("extra_location_moves", [])
        if extra:
            print("\n[1b] Extra location moves (redistributions):")
            generate_extra_moves_file(week, shell_names, out_dir, dry_run)

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
