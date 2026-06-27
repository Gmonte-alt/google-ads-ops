"""
audit_shortname_brands.py  — read-only: which brands can't embed-match?
=======================================================================
The embedded matcher only fires for normalized names of length >= MIN_LEN
(reported as 4). A brand below that threshold (QBE=3, ACE=3, AIG=3, ...) is
INVISIBLE to embedded matching and can only be caught by an explicit variant in
brand_registry_keywords. If such a brand has no adequate variant, queries for it
silently go untagged — that's the QBE gap, generalized.

This lists every sub-threshold brand with its variant coverage so we can add
disambiguated variants for the uncovered ones in a v3.2 patch. It also suggests a
standard variant set per uncovered brand (never the bare short token).

READ-ONLY.

    python audit_shortname_brands.py            # default --min-len 4
    python audit_shortname_brands.py --min-len 4
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

# tokens that make a variant an actual query-catching form (not a bare token)
CONTEXT = ("insurance", "insur", "home", "homeowners", "homeowner", "auto",
           "life", "medicare", "quote", "coverage", "policy", "seguros", "renters")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--min-len", type=int, default=4,
                   help="Embed-match threshold; brands with normalized name shorter than this "
                        "can't embed-match (confirm against the 08-11 tagger constant).")
    return p.parse_args()


def main():
    a = parse_args()
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    brands = conn.execute("""
        SELECT id, brand_name, normalized_name, bid_status, brand_category
        FROM brand_registry
        WHERE LENGTH(normalized_name) < ?
        ORDER BY LENGTH(normalized_name), normalized_name
    """, (a.min_len,)).fetchall()

    print("=" * 78)
    print(f"  Short-name brands (normalized length < {a.min_len}) — can't embed-match")
    print("=" * 78)
    if not brands:
        print("  None. Every brand is long enough to embed-match.")
        conn.close()
        return

    uncovered, thin, covered = [], [], []
    for b in brands:
        variants = conn.execute("""
            SELECT keyword_text, normalized FROM brand_registry_keywords WHERE brand_id = ?
        """, (b["id"],)).fetchall()
        vcount = len(variants)
        # a variant "catches queries" if it's multi-word / contains a context token
        catching = [v for v in variants
                    if any(t in v["normalized"] for t in CONTEXT) or " " in v["normalized"]]
        has_catch = len(catching) > 0

        status = "COVERED" if has_catch else ("THIN" if vcount else "UNCOVERED")
        bucket = {"COVERED": covered, "THIN": thin, "UNCOVERED": uncovered}[status]
        bucket.append(b)

        print(f"\n  {b['brand_name']}  (normalized='{b['normalized_name']}', "
              f"len={len(b['normalized_name'])}, {b['bid_status']})")
        print(f"    variants: {vcount}   catching-variants: {len(catching)}   => {status}")
        if variants:
            sample = ", ".join(v["keyword_text"] for v in variants[:6])
            print(f"    e.g.: {sample}")
        if status != "COVERED":
            nm = b["normalized_name"]
            print(f"    SUGGEST add: '{nm} insurance', '{nm} home insurance', '{nm} insurance quote'  "
                  f"(NOT bare '{nm}')")

    print("\n" + "=" * 78)
    print("  SUMMARY")
    print("=" * 78)
    print(f"  sub-{a.min_len}-char brands: {len(brands)}   "
          f"covered: {len(covered)}   thin: {len(thin)}   UNCOVERED: {len(uncovered)}")
    if uncovered:
        print("\n  UNCOVERED (no query-catching variant — invisible to the tagger):")
        for b in uncovered:
            print(f"     ! {b['brand_name']} ('{b['normalized_name']}')")
    if thin:
        print("\n  THIN (some variants but none clearly query-catching — review):")
        for b in thin:
            print(f"     ? {b['brand_name']} ('{b['normalized_name']}')")
    print("\n  Paste this back — I'll write the v3.2 variant patch for the UNCOVERED/THIN set.")
    conn.close()


if __name__ == "__main__":
    main()
