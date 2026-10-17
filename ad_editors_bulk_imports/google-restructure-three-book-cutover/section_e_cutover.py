"""
section_e_cutover.py — Three-Book Restructure: Final Cutover

Generates:
  section_e_campaign_renames_{date}.csv  — rename 3 live campaigns
  section_e_campaign_pauses_{date}.csv   — pause + retire 4 legacy campaigns

Manual follow-up:
  - Set tROAS 1.10 on Generic, Brand-Home, and CAFL Flagship
  - Set budgets per plan workbook
  - Verify National Flagship is Enabled
  - Post all sections as ONE change set

Usage:
  python section_e_cutover.py --dry-run
  python section_e_cutover.py
"""

import csv
import os
import argparse
from datetime import datetime

OUTPUT_DIR = r"C:\Projects\google-ads-ops\ad_editors_bulk_imports\google-restructure-three-book-cutover\output\section_e"

# ============================================================
# CAMPAIGN RENAMES (3 live campaigns)
# ============================================================

RENAMES = [
    {
        "original": "HOME INSURANCE-TEXAS-NB-SEARCH-ALL-USA-STE",
        "new_name": "HOME INSURANCE-GENERIC-HOME-INTENT-NB-SEARCH-ALL-USA-STE",
        "book": "Generic",
    },
    {
        "original": "HOME INSURANCE_STATE-GROUP_TIER2RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
        "new_name": "HOME INSURANCE_BRAND-HOME-INTENT_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
        "book": "Brand-Home",
    },
    {
        "original": "HOME INSURANCE-CALIFORNIA-NB-SEARCH-ALL-USA-STE",
        "new_name": "HOME INSURANCE_FLAGSHIP_CAFL_TROAS_SEARCH_ALLDEVICE_USA-STE",
        "book": "CAFL Flagship",
    },
]

# ============================================================
# CAMPAIGN PAUSES (4 legacy campaigns → retired)
# ============================================================

RETIREMENTS = [
    {
        "original": "HOME INSURANCE-FLORIDA-NB-SEARCH-ALL-USA-STE",
        "retired_name": "zz-RETIRED-HOME INSURANCE-FLORIDA-NB-SEARCH-ALL-USA-STE",
    },
    {
        "original": "HOME INSURANCE_STATE-GROUP_TIER1RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
        "retired_name": "zz-RETIRED-HOME INSURANCE_STATE-GROUP_TIER1RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
    },
    {
        "original": "HOME INSURANCE_STATE-GROUP_TIER3RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
        "retired_name": "zz-RETIRED-HOME INSURANCE_STATE-GROUP_TIER3RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
    },
    {
        "original": "HOME INSURANCE_STATE-GROUP_TIER4RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
        "retired_name": "zz-RETIRED-HOME INSURANCE_STATE-GROUP_TIER4RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
    },
]


def generate(output_dir, dry_run=False):
    os.makedirs(output_dir, exist_ok=True)
    ds = datetime.now().strftime("%Y-%m-%d")

    # Build rename rows
    rename_rows = []
    for r in RENAMES:
        rename_rows.append({
            "Campaign#Original": r["original"],
            "Campaign": r["new_name"],
            "Campaign Status": "",
        })

    # Build pause + retire rows
    pause_rows = []
    for r in RETIREMENTS:
        pause_rows.append({
            "Campaign#Original": r["original"],
            "Campaign": r["retired_name"],
            "Campaign Status": "Paused",
        })

    # Summary
    print(f"\n{'='*70}")
    print("SECTION E — FINAL CUTOVER")
    print(f"{'='*70}")

    print(f"\n  Campaign renames ({len(rename_rows)}):")
    for r in RENAMES:
        print(f"    [{r['book']}]")
        print(f"    {r['original']}")
        print(f"    → {r['new_name']}")
        print()

    print(f"  Campaign retirements ({len(pause_rows)}):")
    for r in RETIREMENTS:
        print(f"    {r['original']}")
        print(f"    → {r['retired_name']} (Paused)")
        print()

    print(f"{'='*70}")
    print("END STATE (after cutover):")
    print("  LIVE:")
    print("    HOME INSURANCE-GENERIC-HOME-INTENT-NB-SEARCH-ALL-USA-STE")
    print("    HOME INSURANCE_BRAND-HOME-INTENT_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE")
    print("    HOME INSURANCE_FLAGSHIP_CAFL_TROAS_SEARCH_ALLDEVICE_USA-STE")
    print("    HOME INSURANCE_FLAGSHIP_NB_TROAS_SEARCH_ALLDEVICE_USA-STE")
    print("  RETIRED:")
    print("    zz-RETIRED-HOME INSURANCE-FLORIDA-...")
    print("    zz-RETIRED-HOME INSURANCE_STATE-GROUP_TIER1RPL_...")
    print("    zz-RETIRED-HOME INSURANCE_STATE-GROUP_TIER3RPL_...")
    print("    zz-RETIRED-HOME INSURANCE_STATE-GROUP_TIER4RPL_...")

    print(f"\n{'='*70}")
    print("MANUAL STEPS (do in Editor before posting):")
    print("  1. Set tROAS 1.10 on Generic, Brand-Home, and CAFL Flagship")
    print("  2. Set budgets per plan workbook")
    print("  3. Verify National Flagship is Enabled")
    print("  4. Review ALL proposed changes across Sections A-E")
    print("  5. Post everything as ONE change set")

    if dry_run:
        print(f"\n{'='*70}")
        print("DRY RUN — no files written")
        return

    # Write rename file
    rename_path = os.path.join(output_dir, f"section_e_campaign_renames_{ds}.csv")
    headers = ["Campaign#Original", "Campaign", "Campaign Status"]
    with open(rename_path, "w", encoding="utf-16-le", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, delimiter="\t")
        w.writeheader()
        w.writerows(rename_rows)
    print(f"\n  Rename file: {rename_path}")

    # Write pause file
    pause_path = os.path.join(output_dir, f"section_e_campaign_pauses_{ds}.csv")
    with open(pause_path, "w", encoding="utf-16-le", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, delimiter="\t")
        w.writeheader()
        w.writerows(pause_rows)
    print(f"  Pause file:  {pause_path}")

    print(f"\n{'='*70}")
    print("IMPORT ORDER:")
    print("  1. Import section_e_campaign_renames (3 live campaign renames)")
    print("  2. Import section_e_campaign_pauses (4 retirements)")
    print("  3. Set tROAS + budgets manually")
    print("  4. Final review → Post")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Section E: Final Cutover — Renames + Retirements")
    p.add_argument("--output-dir", default=OUTPUT_DIR)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    generate(a.output_dir, a.dry_run)
