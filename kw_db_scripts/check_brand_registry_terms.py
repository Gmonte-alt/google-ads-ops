"""
check_brand_registry_terms.py  — read-only: are these carrier terms new brands
or missing variants of brands already in the registry?
==============================================================================
The carrier-leak batch is two different fixes:
  - NEW BRAND   -> add a brand_registry identity row
  - VARIANT     -> add a brand_registry_keywords row pointing at an EXISTING
                   brand (e.g. 'all state' is a spacing variant of Allstate;
                   adding a new 'All State' brand would fragment/duplicate it)

This classifies each term so the 07 update splits them correctly. For each term
it reports: exact normalized hit in brand_registry, exact hit in
brand_registry_keywords (with the parent brand), and token-overlap candidates
(so 'all state home insurance' surfaces 'Allstate', 'aaa home owners insurance'
surfaces 'AAA', etc.).

READ-ONLY.

    python check_brand_registry_terms.py                 # default built-in list
    python check_brand_registry_terms.py --csv usia_peril_registry_additions.csv
"""

import argparse
import csv
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

# Default built-in list (the original carrier-leak batch) — used only when no
# --csv is passed. The term and, where given, its CSV-supplied normalized form.
DEFAULT_TERMS = [
    "all state home insurance",
    "citizen property insurance corporation",
    "aaa home owners insurance",
    "light house insurance",
    "universal homeowners insurance",
    "american national",
    "qbe",
    "picc",
    "clear spring",
    "garrison",          # annotated parent: USAA
    "ace",               # annotated parent: Chubb
    "metropolitan",      # annotated parent: Farmers
    "american home",     # annotated parent: AIG
]

# generic insurance tokens to ignore when scoring brand-name overlap
STOP = {"insurance", "home", "homeowners", "homeowner", "house", "owners",
        "owner", "property", "corporation", "corp", "the", "of", "and",
        "auto", "co", "company", "inc", "llc", "group", "renters", "renter"}


def normalize(text: str) -> str:
    """Mirror 07_seed_brand_registry.normalize() exactly."""
    return re.sub(r"\s+", " ", text.lower().strip())


def content_tokens(s: str) -> set:
    return {t for t in normalize(s).split() if t not in STOP and len(t) > 1}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default=None,
                   help="CSV of additions to check. Uses 'normalized_name' if present, "
                        "else 'brand_name'. Optional cols carried for context: "
                        "suggested_bid_status, brand_category, rationale.")
    return p.parse_args()


def load_terms_from_csv(path: Path) -> list:
    """Return list of dicts: {term, suggested, category, rationale}. Prefers the
    CSV's normalized_name (several rows are deliberately VARIANT forms, e.g.
    'aaa hazard insurance', not canonical brand names)."""
    if not path.exists():
        sys.exit(f"CSV not found: {path}")
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            term = (row.get("normalized_name") or row.get("brand_name") or "").strip()
            if not term:
                continue
            out.append({
                "term": term,
                "brand_name": (row.get("brand_name") or "").strip(),
                "suggested": (row.get("suggested_bid_status") or "").strip(),
                "category": (row.get("brand_category") or "").strip(),
                "rationale": (row.get("rationale") or "").strip(),
            })
    return out


def main():
    args = parse_args()
    if args.csv:
        rows = load_terms_from_csv(Path(args.csv))
        terms = [r["term"] for r in rows]
        meta = {r["term"]: r for r in rows}
        print(f"  Source: {args.csv}  ({len(terms)} rows)")
    else:
        terms = DEFAULT_TERMS
        meta = {}

    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # Pull the whole registry once for fuzzy candidate scoring.
    brands = conn.execute("""
        SELECT id, brand_name, normalized_name, parent_brand, bid_status, verticals
        FROM brand_registry
    """).fetchall()
    brand_by_norm = {b["normalized_name"]: b for b in brands}
    brand_tokens = {b["id"]: (b, content_tokens(b["brand_name"])) for b in brands}

    print("=" * 78)
    print(f"  Brand-registry term check — {len(brands):,} brands in registry")
    print("=" * 78)

    new_brand, variant_of_existing, ambiguous = [], [], []

    for term in terms:
        nt = normalize(term)
        m = meta.get(term)
        suffix = f"   [csv: {m['suggested']}/{m['category']}]" if m and m["suggested"] else ""
        print(f"\n  TERM: '{term}'{suffix}")

        # 1) exact brand_registry hit
        exact_brand = brand_by_norm.get(nt)
        if exact_brand:
            print(f"    [brand exact]   already a brand: '{exact_brand['brand_name']}' "
                  f"({exact_brand['bid_status']}) — no add needed")
            continue

        # 2) exact brand_registry_keywords (variant) hit
        kw = conn.execute("""
            SELECT k.keyword_text, b.brand_name, b.bid_status
            FROM brand_registry_keywords k
            JOIN brand_registry b ON b.id = k.brand_id
            WHERE k.normalized = ?
        """, (nt,)).fetchall()
        if kw:
            for r in kw:
                print(f"    [variant exact] already a variant of '{r['brand_name']}' "
                      f"({r['bid_status']}) — no add needed")
            continue

        # 3) de-spaced substring: catches spacing-variant misspellings the token
        #    logic misses ('all state' vs 'allstate'). Require brand despaced >= 5
        #    chars to avoid short-name false positives ('ace' inside 'space').
        term_despaced = nt.replace(" ", "")
        despaced_hits = []
        for b in brands:
            bd = b["normalized_name"].replace(" ", "")
            if len(bd) >= 5 and bd in term_despaced:
                despaced_hits.append(b)
        if despaced_hits:
            b = despaced_hits[0]
            print(f"    [variant: spacing] term de-spaced contains existing brand "
                  f"'{b['brand_name']}' ({b['bid_status']}) → VARIANT-add (spacing/misspelling), "
                  f"not a new brand")
            variant_of_existing.append((term, b["brand_name"], b["bid_status"]))
            continue

        # 4) fuzzy: which existing brand do this term's content tokens overlap?
        tt = content_tokens(term)
        scored = []
        for bid, (b, btok) in brand_tokens.items():
            if not btok:
                continue
            # full brand-name token set contained in the term (spacing/embedded case)
            if btok and btok <= tt:
                scored.append((2, b))
            elif btok & tt:
                scored.append((1, b))
        scored.sort(key=lambda x: -x[0])

        if scored and scored[0][0] == 2:
            b = scored[0][1]
            print(f"    [variant?]      term CONTAINS existing brand '{b['brand_name']}' "
                  f"({b['bid_status']}) → likely VARIANT-add to that brand, not a new brand")
            variant_of_existing.append((term, b["brand_name"], b["bid_status"]))
        elif scored:
            cands = ", ".join(f"{b['brand_name']}({b['bid_status']})" for _, b in scored[:4])
            print(f"    [check]         token overlap with: {cands}")
            print(f"                    → decide: variant of one of these, or genuinely new?")
            ambiguous.append((term, cands))
        else:
            print(f"    [new brand]     no registry match → NEW brand_registry identity row")
            new_brand.append(term)

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("  SPLIT SUMMARY")
    print("=" * 78)
    print(f"\n  NEW BRANDS (identity rows to add): {len(new_brand)}")
    for t in new_brand:
        print(f"     + {t}")
    print(f"\n  VARIANT-ADDS (keyword row to EXISTING brand — do NOT create new brand): "
          f"{len(variant_of_existing)}")
    for t, parent, status in variant_of_existing:
        print(f"     ~ '{t}'  → variant of '{parent}' ({status})")
    print(f"\n  AMBIGUOUS (decide before adding): {len(ambiguous)}")
    for t, cands in ambiguous:
        print(f"     ? '{t}'  candidates: {cands}")

    print("\n  Paste this back. NEW BRANDS → BRAND_DATA tuples; VARIANT-ADDS → "
          "brand_registry_keywords\n  rows on the existing brand_id; AMBIGUOUS → we decide each.")
    conn.close()


if __name__ == "__main__":
    main()
