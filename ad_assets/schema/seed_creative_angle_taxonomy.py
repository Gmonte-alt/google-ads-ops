#!/usr/bin/env python
"""
seed_creative_angle_taxonomy.py -- ad_assets/schema/ -- seed the controlled
vocabulary from the V11 workbook (gate ruling 2026-07-18: ONE vocabulary,
V11-sourced).

FINALIZED 2026-07-18 from rsa-load-ready-copy_v11.xlsx and the bulk-import
script's authoritative metadata (CONCEPT_META / UB_META / SWAP_FAMILY).
V11 carries two real dimensions:

  angle_family -- ANGLE FAMILIES (assets.theme_tag validates here; per-headline messaging axis; the "Angle"
                column of Universal Backbone / Headline Swaps and the
                angle_family values in the build script)
  intent_set -- INTENT SETS (per-ad audience/intent framing; the "Intent Set"
                column of RSA Ad-Group Load Asset + tier design)

usp_category and buyer_journey_stage are intentionally UNSEEDED: V11 fuses
USPs into angle descriptions rather than maintaining separate lists. If a
distinct list emerges later, seeding it is a gate item.

Idempotent (INSERT OR REPLACE on angle_code). Dry-run default.

  python seed_creative_angle_taxonomy.py --db "...\\paid_search.db"           # preview
  python seed_creative_angle_taxonomy.py --db "...\\paid_search.db" --apply
"""
import argparse
import os
import sqlite3
import sys

EFFECTIVE = "2026-07-18"

# (angle_code, angle_name, dimension, description)
SEED = [
    # ---- theme_tag: V11 angle families (messaging axis) ----
    ("anchor",     "Vertical / Brand Anchor",   "angle_family",
     "Names the vertical (or brand+product); mandatory in carrier ads per ladder rules"),
    ("comparison", "Comparison / Marketplace",  "angle_family",
     "Side-by-side, insurers-compete, marketplace differentiator (ub:1/9/11)"),
    ("cost",       "Cost / Savings",            "angle_family",
     "Lower premium, beat-the-quote, price position, member-rate compare"),
    ("trust",      "Trust / Risk-Reduction",    "angle_family",
     "Free, no-obligation, no-pressure (ub:4/13, swaps trust lines)"),
    ("proof",      "Social Proof / Quality",    "angle_family",
     "Top-rated, A-rated, highly rated, reviews/ratings framing (ub:5/6/14)"),
    ("speed",      "Speed",                     "angle_family",
     "3-minutes, get-covered-fast (ub:3/12)"),
    ("evaluation", "Evaluation / Fit",          "angle_family",
     "Fit question, how-compare -- upper-funnel deliberation framing"),
    ("coverage",   "Coverage Lookup",           "angle_family",
     "What-does-X-cover / does-X-insure-homes answering"),
    ("renewal",    "Renewal Pain",              "angle_family",
     "Renewal up / rate hike / renewal-too-high trigger"),
    ("segment",    "Segment / Life-Stage",      "angle_family",
     "Mature homeowner 50+ lines (Headline Swaps segment family)"),
    ("geo",        "Geo-Personalization",       "angle_family",
     "{LOCATION(State)} insertion lines"),
    ("dki",        "Keyword Insertion",         "angle_family",
     "{KeyWord:...} lines; band-counting is conditional per Build Script Rules"),

    # ---- intent_set: V11 per-ad intent framings ----
    ("carrier_conquesting",   "Carrier Conquesting",                    "intent_set",
     "Switcher targeting vs named carriers; ladder-resolved [Carrier] lines; bid-status gated"),
    ("carrier_reviews",       "Carrier Reviews",                        "intent_set",
     "Evaluation intent, ratings-and-rates framing (upper-funnel soft sell)"),
    ("coverage_lookup",       "Does Carrier Have (Coverage Lookup)",    "intent_set",
     "Answers implicit coverage question, then pivots to compare"),
    ("aggregator_competitor", "Aggregator-Competitor",                  "intent_set",
     "Searcher already wants a marketplace; real-quotes-not-lists positioning"),
    ("membership_affinity",   "Membership / Affinity",                  "intent_set",
     "AAA/Costco/AARP trust-transfer; compare-beyond-your-membership"),
    ("ca_non_renewal",        "California Non-Renewal & Availability",  "intent_set",
     "Distress/availability intent: dropped, non-renewed, insurer-left-CA"),
    ("generic_backbone",      "Generic Core (Universal Backbone)",      "intent_set",
     "No intent set: DKI+Location adaptive layer plus backbone angles (backbone_plus tier)"),
]


def main():
    p = argparse.ArgumentParser(description="Seed creative_angle_taxonomy from V11 (dry-run default).")
    p.add_argument("--db", required=True)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)

    print("[SEED] %d rows staged from V11:" % len(SEED))
    for code, name, dim, _ in SEED:
        print("  %-24s %-40s %s" % (code, name, dim))

    if not args.apply:
        print("\n[DRY RUN] nothing written. Re-run with --apply to execute.")
        return

    conn = sqlite3.connect(args.db)
    conn.executemany(
        """INSERT OR REPLACE INTO creative_angle_taxonomy
           (angle_code, angle_name, dimension, description, effective_date, source)
           VALUES (?,?,?,?,?, 'V11')""",
        [(c, n, d, desc, EFFECTIVE) for c, n, d, desc in SEED])
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM creative_angle_taxonomy").fetchone()[0]
    conn.close()
    print("\n[APPLY] done -- creative_angle_taxonomy holds %d rows." % n)


if __name__ == "__main__":
    main()
