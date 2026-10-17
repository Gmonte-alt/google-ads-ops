"""
section_a_generic.py — Three-Book Restructure: Section A (Generic Campaign)

Per builder_prompt_v2.md Section A.
Campaign: HOME INSURANCE-TEXAS-NB-SEARCH-ALL-USA-STE (pre-rename)

Generates:
  section_a_keyword_moves_{date}.csv — keyword adds + pauses for quote extraction,
    state moves, and dead variant pauses. Labels on every row.

Manual follow-up (do in Editor after import):
  A.4: Pause ALL Home-NonGeneric-* / Home-Non-Generic-* ad groups (group-level)
  A.5: Pause Home-Generic-Compare + Home-Generic-Cost-Core (fold keys or leave empty)
  A.6: Verify campaign-level EXACT negatives = 104 flagship terms

Usage:
  python section_a_generic.py --dry-run
  python section_a_generic.py
"""

import csv
import os
import argparse
from datetime import datetime

OUTPUT_DIR = r"C:\Projects\google-ads-ops\ad_editors_bulk_imports\google-restructure-three-book-cutover\output\section_a"

CAMPAIGN = "HOME INSURANCE-TEXAS-NB-SEARCH-ALL-USA-STE"

# Labels per LAW 0b
LABEL_MOVE = "restr-2026-09-move"
LABEL_PAUSE = "restr-2026-09-pause"
LABEL_ADD = "restr-2026-09-add"

# ============================================================
# R1/R4: Quote extraction — move 5 keywords to Home-Generic-Quote
# ============================================================

QUOTE_MOVES = [
    # (keyword, match_type, source_ad_group)
    ("home insurance quotes", "Broad", "Home-Generic-Home Insurance-Core"),
    ("home insurance quotes", "Exact", "Home-Generic-Home Insurance-Core"),
    ("homeowners insurance quote", "Broad", "Home-Generic-Home Insurance-Core"),
    ("homeowners insurance quote", "Exact", "Home-Generic-Home Insurance-Core"),
    ("home insurance estimate", "Phrase", "Home-Generic-Cost-Core"),
]
QUOTE_DEST = "Home-Generic-Home Insurance-Quote"

# ============================================================
# R2: Texas state keywords — move to Home-Generic-State
# ============================================================

STATE_MOVES = [
    ("texas home insurance", "Broad", "Home-Generic-Home Insurance-Core"),
    ("texas home insurance", "Exact", "Home-Generic-Home Insurance-Core"),
]
STATE_DEST = "Home-Generic-Home Insurance-State"

# ============================================================
# R3: Dead variant pauses (zero reportable queries since 2026-03-01)
# ============================================================

VARIANT_PAUSES = [
    ("Home Owners Insurance", "Broad", "Home-Generic-Home Insurance-Core"),
    ("home owner insurance", "Broad", "Home-Generic-Home Insurance-Core"),
]


def generate(output_dir, dry_run=False):
    os.makedirs(output_dir, exist_ok=True)
    ds = datetime.now().strftime("%Y-%m-%d")

    rows = []

    # Quote extraction: add to Quote group + pause in source
    for kw, mt, src_ag in QUOTE_MOVES:
        # Add in destination
        rows.append({
            "Campaign": CAMPAIGN,
            "Ad group": QUOTE_DEST,
            "Keyword": kw,
            "Keyword match type": mt,
            "Keyword status": "Enabled",
            "Labels": LABEL_MOVE,
            "Action": "ADD (quote extraction R1/R4)",
        })
        # Pause in source
        rows.append({
            "Campaign": CAMPAIGN,
            "Ad group": src_ag,
            "Keyword": kw,
            "Keyword match type": mt,
            "Keyword status": "Paused",
            "Labels": LABEL_MOVE,
            "Action": "PAUSE source (quote extraction R1/R4)",
        })

    # State moves: add to State group + pause in source
    for kw, mt, src_ag in STATE_MOVES:
        rows.append({
            "Campaign": CAMPAIGN,
            "Ad group": STATE_DEST,
            "Keyword": kw,
            "Keyword match type": mt,
            "Keyword status": "Enabled",
            "Labels": LABEL_MOVE,
            "Action": "ADD (state move R2)",
        })
        rows.append({
            "Campaign": CAMPAIGN,
            "Ad group": src_ag,
            "Keyword": kw,
            "Keyword match type": mt,
            "Keyword status": "Paused",
            "Labels": LABEL_MOVE,
            "Action": "PAUSE source (state move R2)",
        })

    # Dead variant pauses
    for kw, mt, ag in VARIANT_PAUSES:
        rows.append({
            "Campaign": CAMPAIGN,
            "Ad group": ag,
            "Keyword": kw,
            "Keyword match type": mt,
            "Keyword status": "Paused",
            "Labels": LABEL_PAUSE,
            "Action": "PAUSE (dead variant R3)",
        })

    # Summary
    adds = [r for r in rows if r["Keyword status"] == "Enabled"]
    pauses = [r for r in rows if r["Keyword status"] == "Paused"]

    print(f"\n{'='*70}")
    print("SECTION A — GENERIC CAMPAIGN")
    print(f"Campaign: {CAMPAIGN}")
    print(f"{'='*70}")

    print(f"\n  R1/R4 Quote extraction:")
    for kw, mt, src in QUOTE_MOVES:
        print(f"    {kw} [{mt}]: {src} → {QUOTE_DEST}")

    print(f"\n  R2 State moves:")
    for kw, mt, src in STATE_MOVES:
        print(f"    {kw} [{mt}]: {src} → {STATE_DEST}")

    print(f"\n  R3 Dead variant pauses:")
    for kw, mt, ag in VARIANT_PAUSES:
        print(f"    {kw} [{mt}] in {ag}")

    print(f"\n  Totals: {len(adds)} adds + {len(pauses)} pauses = {len(rows)} rows")

    print(f"\n{'='*70}")
    print("MANUAL FOLLOW-UP (do in Editor after import):")
    print("  A.4: Pause ALL Home-NonGeneric-* / Home-Non-Generic-* ad groups")
    print("       (Farmers, Geico, Costco, Hartford, Kin, State Farm, American Family,")
    print("        Group1/3/4/5/6, Home_New — group-level pause, label restr-2026-09-pause)")
    print("  A.5: Pause Home-Generic-Home Insurance-Compare + Home-Generic-Cost-Core")
    print("       (fold their keywords into Other/Core, or leave paused-empty)")
    print("  A.6: Verify campaign-level EXACT negatives = 104 flagship terms (B3 List 1)")

    if dry_run:
        print(f"\n{'='*70}")
        print("DRY RUN — no files written")
        print(f"\nRow-by-row detail:")
        for r in rows:
            print(f"  {r['Keyword status']:8} {r['Keyword']:35} [{r['Keyword match type']:6}] "
                  f"in {r['Ad group']:35} | {r['Action']}")
        return

    # Write import file (Editor columns only — exclude Action metadata)
    path = os.path.join(output_dir, f"section_a_keyword_moves_{ds}.csv")
    headers = ["Campaign", "Ad group", "Keyword", "Keyword match type", "Keyword status", "Labels"]
    with open(path, "w", encoding="utf-16-le", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print(f"\n  File: {path}")
    print(f"  ({len(adds)} adds + {len(pauses)} pauses, UTF-16 LE tab-delimited)")

    print(f"\n{'='*70}")
    print("IMPORT NOTES:")
    print("  - Import this file FIRST, then do the manual steps A.4/A.5/A.6")
    print("  - The pause rows will match existing keywords by Campaign+Ad group+Keyword+Match type")
    print("  - If a pause row creates a new keyword instead of pausing, the keyword text")
    print("    doesn't match exactly — check capitalization and spacing")
    print("  - Gate A check: after all Section A work, only Home-Generic-Home Insurance-* groups should be enabled")
    print("    (Core, Quote, State, Fire, Flood, Other)")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Section A: Generic Campaign Keyword Moves")
    p.add_argument("--output-dir", default=OUTPUT_DIR)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    generate(a.output_dir, a.dry_run)
